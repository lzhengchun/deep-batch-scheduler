from job import Job, Workloads
from cluster import Cluster

import os
import math
import json
import time
import sys
import random
from random import shuffle

import numpy as np
import tensorflow as tf
import scipy.signal

import gym
from gym import spaces
from gym.spaces import Box, Discrete
from gym.utils import seeding

MAX_QUEUE_SIZE = 36
MLP_SIZE = 256

MAX_WAIT_TIME = 12 * 60 * 60 # assume maximal wait time is 12 hours.
MAX_RUN_TIME = 12 * 60 * 60 # assume maximal runtime is 12 hours

# each job has three features: wait_time, requested_node, runtime, machine states,
JOB_FEATURES = 4
DEBUG = False

JOB_SEQUENCE_SIZE = 128
ALGMS_SIZE = 5

def combined_shape(length, shape=None):
    if shape is None:
        return (length,)
    return (length, shape) if np.isscalar(shape) else (length, *shape)

def placeholder(dim=None):
    return tf.placeholder(dtype=tf.float32, shape=combined_shape(None,dim))

def placeholders(*args):
    return [placeholder(dim) for dim in args]

def placeholder_from_space(space):
    if isinstance(space, Box):
        return placeholder(space.shape)
    elif isinstance(space, Discrete):
        return tf.placeholder(dtype=tf.int32, shape=(None,))
    raise NotImplementedError

def placeholders_from_spaces(*args):
    return [placeholder_from_space(space) for space in args]

def get_vars(scope=''):
    return [x for x in tf.trainable_variables() if scope in x.name]

def count_vars(scope=''):
    v = get_vars(scope)
    return sum([np.prod(var.shape.as_list()) for var in v])

def discount_cumsum(x, discount):
    return scipy.signal.lfilter([1], [1, float(-discount)], x[::-1], axis=0)[::-1]


class HPCEnv(gym.Env):
    def __init__(self):  # do nothing and return. A workaround for passing parameters to the environment
        super(HPCEnv, self).__init__()
        print("Initialize Simple HPC Env")

        self.action_space = spaces.Discrete(ALGMS_SIZE)
        self.observation_space = spaces.Box(low=0.0, high=1.0,
                                            shape=(JOB_FEATURES * MAX_QUEUE_SIZE,),
                                            dtype=np.float32)

        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []

        self.current_timestamp = 0
        self.start = 0
        self.next_arriving_job_idx = 0
        self.last_job_in_batch = 0
        self.num_job_in_batch = 0
        self.start_idx_last_reset = 0

        self.loads = None
        self.cluster = None

        self.bsld_algo_dict = {}
        self.scheduled_rl = {}
        self.penalty = 0
        self.scheduled_scores = []

        self.enable_preworkloads = False

        self.algm_fn = [self.sjf_score, self.smallest_score, self.fcfs_score, self.f1_score, self.f2_score]

    def my_init(self, workload_file = '', sched_file = ''):
        print ("loading workloads from dataset:", workload_file)
        self.loads = Workloads(workload_file)
        self.cluster = Cluster("Cluster", self.loads.max_nodes, self.loads.max_procs/self.loads.max_nodes)
        self.penalty_job_score = JOB_SEQUENCE_SIZE * self.loads.max_exec_time / 10

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]
    
    def f1_score(self, job):
        submit_time = job.submit_time
        request_processors = job.request_number_of_processors
        request_time = job.request_time
        # run_time = job.run_time
        assert job.job_id != 0
        return (np.log10(request_processors) * request_time + 870 * np.log10(submit_time))

    def f2_score(self, job):
        submit_time = job.submit_time
        request_processors = job.request_number_of_processors
        request_time = job.request_time
        # run_time = job.run_time
        assert job.job_id != 0
        # f2: r^(1/2)*n + 25600 * log10(s)
        return (np.sqrt(request_time) * request_processors + 25600 * np.log10(submit_time))

    def sjf_score(self, job):
        # run_time = job.run_time
        request_time = job.request_time
        assert job.job_id != 0
        return request_time
    
    def smallest_score(self, job):
        request_processors = job.request_number_of_processors
        assert job.job_id != 0
        return request_processors

    def fcfs_score(self, job):
        submit_time = job.submit_time
        assert job.job_id != 0
        return submit_time

    def reset(self):
        self.cluster.reset()
        self.loads.reset()

        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []

        self.current_timestamp = 0
        self.start = 0
        self.next_arriving_job_idx = 0
        self.last_job_in_batch = 0
        self.num_job_in_batch = 0
        self.scheduled_rl = {}
        self.penalty = 0
        self.scheduled_scores = []

        job_sequence_size = JOB_SEQUENCE_SIZE
        
        # randomly sample a sequence of jobs from workload (self.start_idx_last_reset + 1) % (self.loads.size() - 2 * job_sequence_size)
        self.start = self.np_random.randint(job_sequence_size, (self.loads.size() - job_sequence_size - 1))
        # self.start = 1208
        self.start_idx_last_reset = self.start
        self.num_job_in_batch = job_sequence_size
        self.last_job_in_batch = self.start + self.num_job_in_batch
        self.current_timestamp = self.loads[self.start].submit_time
        self.job_queue.append(self.loads[self.start])
        self.next_arriving_job_idx = self.start + 1

        self.scheduled_scores.append(sum(self.schedule_curr_sequence_reset(self.sjf_score).values()))
        self.scheduled_scores.append(sum(self.schedule_curr_sequence_reset(self.smallest_score).values()))   
        self.scheduled_scores.append(sum(self.schedule_curr_sequence_reset(self.fcfs_score).values()))
        self.scheduled_scores.append(sum(self.schedule_curr_sequence_reset(self.f1_score).values()))
        self.scheduled_scores.append(sum(self.schedule_curr_sequence_reset(self.f2_score).values()))

        return self.build_observation()

    def reset_for_test(self, num):
        self.cluster.reset()
        self.loads.reset()

        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []

        self.current_timestamp = 0
        self.start = 0
        self.next_arriving_job_idx = 0
        self.last_job_in_batch = 0
        self.num_job_in_batch = 0
        self.scheduled_rl = {}
        self.penalty = 0
        self.scheduled_scores = []

        job_sequence_size = num

        self.start = self.np_random.randint(job_sequence_size, (self.loads.size() - job_sequence_size - 1))
        self.start_idx_last_reset = self.start
        self.num_job_in_batch = job_sequence_size
        self.last_job_in_batch = self.start + self.num_job_in_batch
        self.current_timestamp = self.loads[self.start].submit_time
        self.job_queue.append(self.loads[self.start])
        self.next_arriving_job_idx = self.start + 1

    def schedule_curr_sequence_reset(self, score_fn):
        # schedule the sequence of jobs using heuristic algorithm. 
        scheduled_logs = {}
        while True:
            self.job_queue.sort(key=lambda j: score_fn(j))
            job_for_scheduling = self.job_queue[0]

            # make sure we move forward and release needed resources
            if not self.cluster.can_allocated(job_for_scheduling):
                self.moveforward_for_resources(job_for_scheduling)
            
            assert job_for_scheduling.scheduled_time == -1  # this job should never be scheduled before.

            job_for_scheduling.scheduled_time = self.current_timestamp
            job_for_scheduling.allocated_machines = self.cluster.allocate(job_for_scheduling.job_id,
                                                                            job_for_scheduling.request_number_of_processors)
            self.running_jobs.append(job_for_scheduling)
            score = (self.job_score(job_for_scheduling) / self.num_job_in_batch)  # calculated reward
            scheduled_logs[job_for_scheduling.job_id] = score
            self.job_queue.remove(job_for_scheduling)

            not_empty = self.moveforward_for_job()

            if not not_empty:
                break

        # reset again
        self.cluster.reset()
        self.loads.reset()
        self.job_queue = []
        self.running_jobs = []
        self.visible_jobs = []
        self.pairs = []
        self.current_timestamp = self.loads[self.start].submit_time
        self.job_queue.append(self.loads[self.start])
        self.last_job_in_batch = self.start + self.num_job_in_batch
        self.next_arriving_job_idx = self.start + 1

        return scheduled_logs

    def build_observation(self):
        vector = np.zeros((MAX_QUEUE_SIZE) * JOB_FEATURES, dtype=float)
        self.job_queue.sort(key=lambda job: self.sjf_score(job))
        self.visible_jobs = []
        for i in range(0, MAX_QUEUE_SIZE):
            if i < len(self.job_queue):
                self.visible_jobs.append(self.job_queue[i])
            else:
                break
        self.visible_jobs.sort(key=lambda j: self.sjf_score(j))
        # random.shuffle(self.visible_jobs)

        self.pairs = []
        for i in range(0, MAX_QUEUE_SIZE): # always keep the last job as "do nothing and wait"
            if i < len(self.visible_jobs):
                job = self.visible_jobs[i]
                submit_time = job.submit_time
                request_processors = job.request_number_of_processors
                request_time = job.request_time
                # run_time = job.run_time
                wait_time = self.current_timestamp - submit_time

                # make sure that larger value is better.
                normalized_wait_time = min(float(wait_time) / float(MAX_WAIT_TIME), 1.0 - 1e-8)
                normalized_run_time = min(float(request_time) / float(self.loads.max_exec_time), 1.0 - 1e-8)
                normalized_request_nodes = min(float(request_processors) / float(self.loads.max_procs), 1.0 - 1e-8)
                if self.cluster.can_allocated(job):
                    can_schedule_now = 1
                else:
                    can_schedule_now = 0
                self.pairs.append([job,normalized_wait_time, normalized_run_time, normalized_request_nodes, can_schedule_now])        
            else:
                self.pairs.append([None,0,1,1,0])

        # random.shuffle(self.pairs)   # agent sees jobs in random order

        for i in range(0, MAX_QUEUE_SIZE):
            vector[i*JOB_FEATURES:(i+1)*JOB_FEATURES] = self.pairs[i][1:]

        return vector

    def moveforward_for_resources(self, job):
        while not self.cluster.can_allocated(job):
            assert self.running_jobs
            self.running_jobs.sort(key=lambda running_job: (running_job.scheduled_time + running_job.run_time))
            next_resource_release_time = (self.running_jobs[0].scheduled_time + self.running_jobs[0].run_time)
            next_resource_release_machines = self.running_jobs[0].allocated_machines
                
            if self.next_arriving_job_idx < self.last_job_in_batch \
            and self.loads[self.next_arriving_job_idx].submit_time <= next_resource_release_time:
                assert self.current_timestamp <= self.loads[self.next_arriving_job_idx].submit_time
                self.current_timestamp = self.loads[self.next_arriving_job_idx].submit_time
                self.job_queue.append(self.loads[self.next_arriving_job_idx])
                self.next_arriving_job_idx += 1
            else:
                assert self.current_timestamp <= next_resource_release_time
                self.current_timestamp = next_resource_release_time
                self.cluster.release(next_resource_release_machines)
                self.running_jobs.pop(0)  # remove the first running job
    
    def moveforward_for_job(self):
        if self.job_queue:
            return True

        # if we need to add job, but can not add any more, return False indicating the job_queue is for sure empty now.
        if self.next_arriving_job_idx >= self.last_job_in_batch:
            return False

        # move forward to add jobs into job queue.
        while not self.job_queue:
            if not self.running_jobs:  # there are no running jobs
                next_resource_release_time = sys.maxsize  # always add jobs if no resource can be released.
                next_resource_release_machines = []
            else:
                self.running_jobs.sort(key=lambda running_job: (running_job.scheduled_time + running_job.run_time))
                next_resource_release_time = (self.running_jobs[0].scheduled_time + self.running_jobs[0].run_time)
                next_resource_release_machines = self.running_jobs[0].allocated_machines

            if self.loads[self.next_arriving_job_idx].submit_time <= next_resource_release_time:
                assert self.current_timestamp <= self.loads[self.next_arriving_job_idx].submit_time
                self.current_timestamp = self.loads[self.next_arriving_job_idx].submit_time
                self.job_queue.append(self.loads[self.next_arriving_job_idx])
                self.next_arriving_job_idx += 1
                return True     # job added
            else:
                assert self.current_timestamp <= next_resource_release_time
                self.current_timestamp = next_resource_release_time
                self.cluster.release(next_resource_release_machines)
                self.running_jobs.pop(0)  # remove the first running job.
            
    def job_score(self, job_for_scheduling):
        _tmp = max(1.0, (float(job_for_scheduling.scheduled_time - job_for_scheduling.submit_time + job_for_scheduling.run_time)
                        /
                        max(job_for_scheduling.run_time, 10)))
        # Weight larger jobs.
        #_tmp = _tmp * (job_for_scheduling.run_time * job_for_scheduling.request_number_of_processors)
        return _tmp

    def has_only_one_job(self):
        if len(self.job_queue) == 1:
            return True
        else:
            return False

    def schedule(self, job_for_scheduling):

        # make sure we move forward and release needed resources
        if not self.cluster.can_allocated(job_for_scheduling):
            self.moveforward_for_resources(job_for_scheduling)

        # we should be OK to schedule the job now
        assert job_for_scheduling.scheduled_time == -1  # this job should never be scheduled before.
        job_for_scheduling.scheduled_time = self.current_timestamp
        job_for_scheduling.allocated_machines = self.cluster.allocate(job_for_scheduling.job_id, job_for_scheduling.request_number_of_processors)
        self.running_jobs.append(job_for_scheduling)

        score = (self.job_score(job_for_scheduling) / self.num_job_in_batch)  # calculated reward
        self.scheduled_rl[job_for_scheduling.job_id] = score
        self.job_queue.remove(job_for_scheduling)  # remove the job from job queue

        # after scheduling, check if job queue is empty, try to add jobs. 
        not_empty = self.moveforward_for_job()

        if not_empty:
            # job_queue is not empty
            return False
        else:
            # job_queue is empty and can not add new jobs as we reach the end of the sequence
            # f1_total = sum(self.scheduled_f1.values())
            # rl_total = sum(self.scheduled_rl.values())
            return True

    def valid(self, a):
        action = a[0]
        return self.pairs[action][0]
        
    def step(self, a):
        fn = self.algm_fn[a]
        self.visible_jobs.sort(key=lambda j: fn(j))
        job_for_scheduling = self.visible_jobs[0]
        done = self.schedule(job_for_scheduling)

        if not done:
            obs = self.build_observation()
            return [obs, 0, False, None]
        else:
            rl_total = sum(self.scheduled_rl.values())
            #print (self.scheduled_f1)
            #print ("------------------------")
            #print (self.scheduled_rl)
            best_total = min(self.scheduled_scores) #self.scheduled_scores[0]
            # rwd = (best_total - rl_total)
            if best_total < rl_total:
                rwd = -1
            if best_total > rl_total:
                rwd = 1
            if best_total == rl_total:
                rwd = 0
            return [None, rwd, True, None]
    
    def step_for_test(self, a):
        fn = self.algm_fn[a]
        self.visible_jobs.sort(key=lambda j: fn(j))
        job_for_scheduling = self.visible_jobs[0]
        done = self.schedule(job_for_scheduling)

        # if there is only one job, schedule it and move forward
        while not done and self.has_only_one_job():
            done = self.schedule(self.job_queue[0])

        if not done:
            obs = self.build_observation()
            return [obs, 0, False, None]
        else:
            rl_total = sum(self.scheduled_rl.values())
            return [None, rl_total, True, None]

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--workload', type=str, default='./data/lublin_256.swf')  # RICC-2010-2
    args = parser.parse_args()
    current_dir = os.getcwd()
    workload_file = os.path.join(current_dir, args.workload)

    env = HPCEnv()
    env.my_init(workload_file=workload_file, sched_file=workload_file)
    env.seed(int(time.time()))

    for _ in range(100):
        _, r = env.reset(), 0
        print (env.scheduled_scores, end="\t")

        while True:
            _, r, d, _ = env.step(0)
            if d:
                print ("Final Reward:", r)
                break
