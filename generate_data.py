import os
import sys
import glob
import gzip
import json
import ecole
import queue
import pickle
import shutil
import argparse
import threading
import numpy as np
from pathlib import Path
import configparser
import torch
from common.environments import Branching as Environment
from agent_model import GNNPolicyItem, GNNPolicyLoad, GNNPolicyAno


cf = configparser.ConfigParser()
cf.read("./configs/dataset.ini")
DATASET_DIR = cf.get("dataset", "DATASET_DIR")

# import environment
STORE_DIR = cf.get("dataset", "STORE_DIR")

sys.path.append(DATASET_DIR)
# parameters
NODE_RECORD_PROB = cf.getfloat("dataset", "NODE_RECORD_PROB")
TIME_LIMIT = cf.getint("dataset", "TIME_LIMIT")
POLICY_TYPE = cf.getint("dataset", "POLICY_TYPE")
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

if POLICY_TYPE == 0:
    policy = GNNPolicyItem().to(DEVICE)
elif POLICY_TYPE == 1:
    policy = GNNPolicyLoad().to(DEVICE)
elif POLICY_TYPE == 2:
    policy = GNNPolicyAno().to(DEVICE)
else:
    raise NotImplementedError


class ExploreThenStrongBranch:
    """
    Custom observation function.
    Queries the expert with a given probability. Returns variable scores given
    by the expert (if queried), or pseudocost scores otherwise.
    Parameters
    ----------
    expert_probability : float in [0, 1]
        Probability of running the expert strategy and collecting samples.
    """

    def __init__(self, expert_probability):
        self.expert_probability = expert_probability
        self.pseudocosts_function = ecole.observation.Pseudocosts()
        self.strong_branching_function = ecole.observation.StrongBranchingScores()

    def before_reset(self, model):
        """
        Reset internal data at the start of episodes.
        Called before environment dynamics are reset.
        Parameters
        ----------
        model : ecole.scip.Model
            Model defining the current state of the solver.
        """
        self.pseudocosts_function.before_reset(model)
        self.strong_branching_function.before_reset(model)

    def extract(self, model, done):
        """
        Extract the observation on the given state.
        Parameters
        ----------
        model : ecole.scip.Model
            Model defining the current state of the solver.
        done : bool
            Flag indicating if the state is terminal.
        Returns
        -------
        scores : np.ndarray
            Variable scores.
        scores_are_expert : bool
            Flag indicating whether scores are given by the expert.
        """
        probabilities = [1 - self.expert_probability, self.expert_probability]
        expert_chosen = bool(np.random.choice(np.arange(2), p=probabilities))
        if expert_chosen:
            return (self.strong_branching_function.extract(model, done), True)
        else:
            return (self.pseudocosts_function.extract(model, done), False)


def send_orders(
    orders_queue, instances, seed, query_expert_prob, time_limit, out_dir, stop_flag
):
    """
    Continuously send sampling orders to workers (relies on limited
    queue capacity).
    Parameters
    ----------
    orders_queue : queue.Queue
        Queue to which to send orders.
    instances : list
        Instance file names from which to sample episodes.
    seed : int
        Random seed for reproducibility.
    query_expert_prob : float in [0, 1]
        Probability of running the expert strategy and collecting samples.
    time_limit : float in [0, 1e+20]
        Maximum running time for an episode, in seconds.
    out_dir: str
        Output directory in which to write samples.
    stop_flag: threading.Event
        A flag to tell the thread to stop.
    """
    rng = np.random.RandomState(seed)

    episode = 0
    while not stop_flag.is_set():
        instance = Path(rng.choice(instances))
        with open(instance.with_name(instance.stem).with_suffix(".json")) as f:
            instance_info = json.load(f)
        initial_primal_bound = instance_info["primal_bound"]
        seed = rng.randint(2 ** 32)
        orders_queue.put(
            [
                episode,
                instance,
                initial_primal_bound,
                seed,
                query_expert_prob,
                time_limit,
                out_dir,
            ]
        )
        episode += 1


def make_samples(in_queue, out_queue, stop_flag):
    """
    Worker loop: fetch an instance, run an episode and record samples.
    Parameters
    ----------
    in_queue : queue.Queue
        Input queue from which orders are received.
    out_queue : queue.Queue
        Output queue in which to send samples.
    stop_flag: threading.Event
        A flag to tell the thread to stop.
    """
    sample_counter = 0
    while not stop_flag.is_set():
        (
            episode,
            instance,
            initial_primal_bound,
            seed,
            query_expert_prob,
            time_limit,
            out_dir,
        ) = in_queue.get()

        observation_function = {
            "scores": ExploreThenStrongBranch(expert_probability=query_expert_prob),
            "node_observation": ecole.observation.NodeBipartite(),
        }
        env = Environment(
            time_limit=time_limit, observation_function=observation_function,
        )

        print(
            f"[w {threading.current_thread().name}] episode {episode}, seed {seed}, "
            f"processing instance '{instance}'...\n",
            end="",
        )
        out_queue.put(
            {"type": "start", "episode": episode, "instance": instance, "seed": seed,}
        )

        env.seed(seed)
        observation, action_set, _, done, _ = env.reset(
            str(instance), objective_limit=initial_primal_bound
        )
        while not done:
            scores, scores_are_expert = observation["scores"]
            node_observation = observation["node_observation"]
            constraint_features, edge_indices, edge_features, variable_features = (
                node_observation.row_features.astype(np.float32),
                node_observation.edge_features.indices.astype(np.int64),
                node_observation.edge_features.values.astype(np.float32),
                node_observation.column_features.astype(np.float32),
            )
            node_observation = (
                node_observation.row_features,
                (
                    node_observation.edge_features.indices,
                    node_observation.edge_features.values,
                ),
                node_observation.column_features,
            )

            action = action_set[scores[action_set].argmax()]
            if scores_are_expert == False:                     # 用我们训练出来的action
                variable_features = np.delete(variable_features, 14, axis=1)
                variable_features = np.delete(variable_features, 13, axis=1)

                constraint_features = torch.FloatTensor(constraint_features).to(DEVICE)
                edge_indices = torch.LongTensor(edge_indices.astype(np.int32)).to(
                    DEVICE
                )
                edge_features = torch.FloatTensor(
                    np.expand_dims(edge_features, axis=-1)
                ).to(DEVICE)
                variable_features = torch.FloatTensor(variable_features).to(DEVICE)
                with torch.no_grad():
                    observation_tensor = (
                        constraint_features,
                        edge_indices,
                        edge_features.view(-1, 1),
                        variable_features,
                    )
                    logits = policy(*observation_tensor)
                    action = action_set[logits[action_set.astype(np.int64)].argmax()]

            if scores_are_expert and not stop_flag.is_set():
                data = [node_observation, action, action_set, scores]
                filename = f"{out_dir}/sample_{episode}_{sample_counter}.pkl"

                with gzip.open(filename, "wb") as f:
                    pickle.dump(
                        {
                            "episode": episode,
                            "instance": instance,
                            "seed": seed,
                            "data": data,
                        },
                        f,
                    )
                out_queue.put(
                    {
                        "type": "sample",
                        "episode": episode,
                        "instance": instance,
                        "seed": seed,
                        "filename": filename,
                    }
                )
                sample_counter += 1

            try:
                observation, action_set, _, done, _ = env.step(action)
            except Exception as e:
                done = True
                with open("error_log.txt", "a") as f:
                    f.write(f"Error occurred solving {instance} with seed {seed}\n")
                    f.write(f"{e}\n")

        print(
            f"[w {threading.current_thread().name}] episode {episode} done, {sample_counter} samples\n",
            end="",
        )
        out_queue.put(
            {"type": "done", "episode": episode, "instance": instance, "seed": seed,}
        )




# 核心函数
def collect_samples(
    instances, out_dir, rng, n_samples, n_jobs, query_expert_prob, time_limit
):
    """
    Runs branch-and-bound episodes on the given set of instances, and collects
    randomly (state, action) pairs from the 'vanilla-fullstrong' expert
    brancher.
    Parameters
    ----------
    instances : list
        Instance files from which to collect samples.
    out_dir : str
        Directory in which to write samples.
    rng : numpy.random.RandomState
        A random number generator for reproducibility.
    n_samples : int
        Number of samples to collect.
    n_jobs : int
        Number of jobs for parallel sampling.
    query_expert_prob : float in [0, 1]
        Probability of using the expert policy and recording a (state, action)
        pair.
    time_limit : float in [0, 1e+20]
        Maximum running time for an episode, in seconds.
    """
    os.makedirs(out_dir, exist_ok=True)

    # start workers
    orders_queue = queue.Queue(maxsize=2 * n_jobs)
    answers_queue = queue.Queue()

    tmp_samples_dir = f"{out_dir}/tmp"
    os.makedirs(tmp_samples_dir, exist_ok=True)

    # start dispatcher
    dispatcher_stop_flag = threading.Event()
    dispatcher = threading.Thread(
        target=send_orders,
        args=(
            orders_queue,
            instances,
            rng.randint(2 ** 32),
            query_expert_prob,
            time_limit,
            tmp_samples_dir,
            dispatcher_stop_flag,
        ),
        daemon=True,
    )
    dispatcher.start()

    workers = []
    workers_stop_flag = threading.Event()
    for i in range(n_jobs):
        p = threading.Thread(
            target=make_samples,
            args=(orders_queue, answers_queue, workers_stop_flag),
            daemon=True,
        )
        workers.append(p)
        p.start()

    # record answers and write samples
    buffer = {}
    current_episode = 0
    i = 0
    in_buffer = 0
    while i < n_samples:
        sample = answers_queue.get()

        # add received sample to buffer
        if sample["type"] == "start":
            buffer[sample["episode"]] = []
        else:
            buffer[sample["episode"]].append(sample)
            if sample["type"] == "sample":
                in_buffer += 1

        # if any, write samples from current episode
        while current_episode in buffer and buffer[current_episode]:
            samples_to_write = buffer[current_episode]
            buffer[current_episode] = []

            for sample in samples_to_write:

                # if no more samples here, move to next episode
                if sample["type"] == "done":
                    del buffer[current_episode]
                    current_episode += 1

                # else write sample
                else:
                    os.rename(sample["filename"], f"{out_dir}/sample_{i+1}.pkl")
                    in_buffer -= 1
                    i += 1
                    print(
                        f"[m {threading.current_thread().name}] {i} / {n_samples} samples written, "
                        f"ep {sample['episode']} ({in_buffer} in buffer).\n",
                        end="",
                    )

                    # early stop dispatcher
                    if in_buffer + i >= n_samples and dispatcher.is_alive():
                        dispatcher_stop_flag.set()
                        print(
                            f"[m {threading.current_thread().name}] dispatcher stopped...\n",
                            end="",
                        )

                    # as soon as enough samples are collected, stop
                    if i == n_samples:
                        buffer = {}
                        break

    # # stop all workers
    workers_stop_flag.set()

    shutil.rmtree(tmp_samples_dir, ignore_errors=True)


















if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "problem",
        help="MILP instance type to process.",
        choices=["item_placement", "load_balancing", "anonymous"],
    )
    parser.add_argument(
        "-s", "--seed", help="Random generator seed.", type=int, default=0,
    )
    parser.add_argument(
        "--file_count", help="Random generator seed.", type=int, default=1,
    )
    parser.add_argument(
        "--train_size", help="Random generator seed.", type=int, default=100,
    )
    parser.add_argument(
        "--valid_size", help="Random generator seed.", type=int, default=100,
    )
    parser.add_argument(
        "-j", "--njobs", help="Number of parallel jobs.", type=int, default=1,
    )
    args = parser.parse_args()

    print(f"seed {args.seed}")

    # get instances
    if args.problem == "item_placement":
        instances_train = glob.glob(
            f"{DATASET_DIR}/1_item_placement/train/*.mps.gz"
        )
        instances_valid = glob.glob(
            f"{DATASET_DIR}/1_item_placement/valid/*.mps.gz"
        )
        out_dir = f"{STORE_DIR}/samples/1_item_placement_dagger{args.file_count}"

    elif args.problem == "load_balancing":
        instances_train = glob.glob(
            f"{DATASET_DIR}/2_load_balancing/train/*.mps.gz"
        )
        instances_valid = glob.glob(
            f"{DATASET_DIR}/2_load_balancing/valid/*.mps.gz"
        )
        out_dir = f"{STORE_DIR}/samples/2_load_balancing_dagger{args.file_count}"

    elif args.problem == "anonymous":
        instances_train = glob.glob(
            f"{DATASET_DIR}/3_anonymous/train/*.mps.gz"
        )
        instances_valid = glob.glob(
            f"{DATASET_DIR}/3_anonymous/valid/*.mps.gz"
        )
        out_dir = f"{STORE_DIR}/samples/3_anonymous_dagger{args.file_count}"

    else:
        raise NotImplementedError
    # write config file

    print(f"{len(instances_train)} train instances for {args.train_size} samples")
    print(f"{len(instances_valid)} validation instances for {args.valid_size} samples")
    if args.problem == "item_placement":
        print(
            f"{STORE_DIR}/train_files/1_item_placement_dagger{args.file_count-1}/best_params.pkl"
        )
        if os.path.exists(
            f"{STORE_DIR}/train_files/1_item_placement_dagger{args.file_count-1}/best_params.pkl"
        ):
            policy.load_state_dict(
                torch.load(
                    f"{STORE_DIR}/train_files/1_item_placement_dagger{args.file_count-1}/best_params.pkl"
                )
            )
            print("load dict")
    if args.problem == "load_balancing":
        if os.path.exists(
            f"{STORE_DIR}/train_files/2_load_placement_dagger{args.file_count-1}/best_params.pkl"
        ):
            policy.load_state_dict(
                torch.load(
                    f"{STORE_DIR}/train_files/2_load_placement_dagger{args.file_count-1}/load_balancing/best_params.pkl"
                )
            )
            print("load dict")
    if args.problem == "anonymous":
        if os.path.exists(
            f"{STORE_DIR}/train_files/3_anonymous_dagger{args.file_count-1}/best_params.pkl"
        ):
            policy.load_state_dict(
                torch.load(
                    f"{STORE_DIR}/train_files/3_anonymous_dagger{args.file_count-1}/best_params.pkl"
                )
            )
            print("load dict")
    # create output directory, throws an error if it already exists
    os.makedirs(out_dir)

    cf.write(open(f"{out_dir}/dataset.ini", "w"))

    # generate train samples
    rng = np.random.RandomState(args.seed + 100)

    collect_samples(
        instances_train,
        out_dir + "/train",
        rng,
        args.train_size,
        args.njobs,
        query_expert_prob=NODE_RECORD_PROB,
        time_limit=TIME_LIMIT,
    )

    # generate validation samples
    rng = np.random.RandomState(args.seed + 1)
    collect_samples(
        instances_valid,
        out_dir + "/valid",
        rng,
        args.valid_size,
        args.njobs,
        query_expert_prob=NODE_RECORD_PROB,
        time_limit=TIME_LIMIT,
    )
