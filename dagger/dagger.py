import sys
import time
import project_root
import numpy as np
import tensorflow as tf
import datetime
from tensorflow import contrib
from os import path
from models import DaggerLSTM
from experts import TrueDaggerExpert
from env.sender import Sender
from helpers.helpers import (
    make_sure_path_exists, normalize, one_hot, curr_ts_ms, get_open_udp_port)
from subprocess import check_output


class Status:
    EP_DONE = 0
    WORKER_DONE = 1
    WORKER_START = 2
    PS_DONE = 3


class DaggerLeader(object):
    def __init__(self, cluster, server, num_hosts, worker_tasks):
        self.cluster = cluster
        self.server = server
        self.worker_tasks = worker_tasks
        self.num_hosts = num_hosts
        self.aggregated_states = []
        self.aggregated_actions = []
        self.max_eps = 1000
        self.checkpoint_delta = 10
        self.checkpoint = self.checkpoint_delta
        self.learn_rate = 7e-5
        self.regularization_lambda = 1e-4
        self.train_step = 0

        self.state_dim = Sender.state_dim
        self.action_cnt = Sender.action_cnt
        self.aug_state_dim = self.state_dim + self.action_cnt

        # Create the master network and training/sync queues
        with tf.variable_scope('global'):
            self.global_network = DaggerLSTM(
                state_dim=self.aug_state_dim, action_cnt=self.action_cnt)

        self.leader_device_cpu = '/job:ps/task:0/cpu:0'
        with tf.device(self.leader_device_cpu):
            with tf.variable_scope('global_cpu'):
                self.global_network_cpu = DaggerLSTM(
                    state_dim=self.aug_state_dim, action_cnt=self.action_cnt)

        cpu_vars = self.global_network_cpu.trainable_vars
        gpu_vars = self.global_network.trainable_vars
        self.sync_op = tf.group(*[v1.assign(v2) for v1, v2 in zip(
            cpu_vars, gpu_vars)])

        self.default_batch_size = 90
        self.default_init_state = self.global_network.zero_init_state(
                self.default_batch_size)

        # Each element is [[aug_state]], [action]
        self.train_q = tf.FIFOQueue(
                self.num_hosts, [tf.float32, tf.int32],
                shared_name='training_feed')

        # Keys: worker indices, values: Tensorflow messaging queues
        # Queue Elements: Status message
        self.sync_queues = {}
        for idx in worker_tasks:
            queue_name = 'sync_q_%d' % idx
            self.sync_queues[idx] = tf.FIFOQueue(3, [tf.int16],
                                                 shared_name=queue_name)

        self.setup_tf_ops()

        self.sess = tf.Session(
            server.target, config=tf.ConfigProto(allow_soft_placement=True))
        self.sess.run(tf.global_variables_initializer())

    def cleanup(self):
        """ Sends messages to workers to stop and saves the model. """
        for idx in self.worker_tasks:
            self.sess.run(self.sync_queues[idx].enqueue(Status.PS_DONE))
        self.save_model()

    def save_model(self, checkpoint=None):
        """ Takes care of saving/checkpointing the model. """
        if checkpoint is None:
            model_path = path.join(self.logdir, 'model')
        else:
            model_path = path.join(self.logdir, 'checkpoint-%d' % checkpoint)

        # save parameters to parameter server
        saver = tf.train.Saver(self.global_network.trainable_vars)
        saver.save(self.sess, model_path)
        sys.stderr.write('\nModel saved to param. server at %s\n' % model_path)

    def setup_tf_ops(self):
        """ Sets up Tensorboard operators and tools, such as the optimizer,
        summary values, Tensorboard, and Session.
        """

        self.actions = tf.placeholder(tf.int32, [None, None])

        reg_loss = 0.0
        for x in self.global_network.trainable_vars:
            reg_loss += tf.nn.l2_loss(x)
        reg_loss *= self.regularization_lambda

        cross_entropy_loss = tf.reduce_mean(
                tf.nn.sparse_softmax_cross_entropy_with_logits(
                    labels=self.actions,
                    logits=self.global_network.action_scores))

        self.total_loss = cross_entropy_loss + reg_loss

        optimizer = tf.train.AdamOptimizer(self.learn_rate)
        self.train_op = optimizer.minimize(self.total_loss)

        tf.summary.scalar('reduced_ce_loss', cross_entropy_loss)
        tf.summary.scalar('reg_loss', reg_loss)
        tf.summary.scalar('total_loss', self.total_loss)
        self.summary_op = tf.summary.merge_all()

        git_commit = check_output(
                'cd %s && git rev-parse @' % project_root.DIR, shell=True)
        date_time = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
        log_name = date_time + '-%s' % git_commit.strip()
        self.logdir = path.join(project_root.DIR, 'dagger', 'logs', log_name)
        make_sure_path_exists(self.logdir)
        self.summary_writer = tf.summary.FileWriter(self.logdir)

    def wait_on_workers(self):
        """ Update which workers are done or dead. Stale tokens will
        eventually be cleaned out.
        Returns the number of workers that finished their episode.
        """
        workers_ep_done = 0
        while workers_ep_done < len(self.worker_tasks):
            # Let the workers dequeue their start tokens
            time.sleep(0.5)

            # check in each queue for worker messages and update workers
            workers_done = []
            for idx in self.worker_tasks:
                worker_queue = self.sync_queues[idx]
                msg = self.sess.run(worker_queue.dequeue())

                if msg == Status.EP_DONE:
                    workers_ep_done += 1
                elif msg == Status.WORKER_DONE:
                    workers_done.append(idx)
                    self.sess.run(worker_queue.close())
                else:
                    self.sess.run(worker_queue.enqueue(msg))

            for worker in workers_done:
                self.worker_tasks.remove(worker)

        return workers_ep_done

    def run_one_train_step(self, batch_states, batch_actions):
        """ Runs one step of the training operator on the given data.
        At times will update Tensorboard and save a checkpointed model.
        Returns the total loss calculated.
        """
        summary = True if self.train_step % 10 == 0 else False

        ops_to_run = [self.train_op, self.total_loss]

        if summary:
            ops_to_run.append(self.summary_op)

        pi = self.global_network

        start_ts = curr_ts_ms()
        ret = self.sess.run(ops_to_run, feed_dict={
            pi.input: batch_states,
            self.actions: batch_actions,
            pi.state_in: self.init_state})

        elapsed = (curr_ts_ms() - start_ts) / 1000.0
        sys.stderr.write('train step %d: time %.2f\n' %
                         (self.train_step, elapsed))

        if summary:
            self.summary_writer.add_summary(ret[2], self.train_step)

        return ret[1]

    def train(self):
        """ Runs the training operator until the loss converges. """
        curr_iter = 0

        min_loss = float('inf')
        iters_since_min_loss = 0

        batch_size = min(len(self.aggregated_states), self.default_batch_size)
        num_batches = len(self.aggregated_states) / batch_size

        if batch_size != self.default_batch_size:
            self.init_state = self.global_network.zero_init_state(batch_size)
        else:
            self.init_state = self.default_init_state

        while True:
            curr_iter += 1

            mean_loss = 0.0
            max_loss = 0.0

            for batch_num in xrange(num_batches):
                self.train_step += 1

                start = batch_num * batch_size
                end = start + batch_size

                batch_states = self.aggregated_states[start:end]
                batch_actions = self.aggregated_actions[start:end]

                loss = self.run_one_train_step(batch_states, batch_actions)

                mean_loss += loss
                max_loss = max(loss, max_loss)

            mean_loss /= num_batches

            sys.stderr.write('--- iter %d: max loss %.4f, mean loss %.4f\n' %
                             (curr_iter, max_loss, mean_loss))

            if max_loss < min_loss - 0.001:
                min_loss = max_loss
                iters_since_min_loss = 0
            else:
                iters_since_min_loss += 1

            if curr_iter > 50:
                break

            if iters_since_min_loss >= max(0.2 * curr_iter, 10):
                break

    def run(self, debug=False):
        for curr_ep in xrange(self.max_eps):
            if debug:
                sys.stderr.write('[PSERVER EP %d]: waiting for workers %s\n' %
                                 (curr_ep, self.worker_tasks))

            workers_ep_done = self.wait_on_workers()

            # If workers had data, dequeue ALL the samples and train
            if workers_ep_done > 0:

                num_samples = self.sess.run(self.train_q.size())
                while num_samples != 0:
                    data = self.sess.run(self.train_q.dequeue())
                    self.aggregated_states.append(data[0])
                    self.aggregated_actions.append(data[1])
                    num_samples = self.sess.run(self.train_q.size())

                if debug:
                    sys.stderr.write('[PSERVER]: start training\n')

                self.train()

                if debug:
                    gpu_vars = self.global_network.trainable_vars[0][0][0]
                    cpu_vars = self.global_network_cpu.trainable_vars[0][0][0]

                    print '[PSERVER]: Network after training:'
                    print '[PSERVER]: GPU vars', self.sess.run(gpu_vars)
                    print '[PSERVER]: CPU vars', self.sess.run(cpu_vars)
                    # copy trained variables from GPU to CPU
                    self.sess.run(self.sync_op)
                    print '[PSERVER]: After sync'
                    print '[PSERVER]: CPU vars', self.sess.run(cpu_vars)
                    sys.stdout.flush()

            else:
                if debug:
                    sys.stderr.write('[PSERVER]: quitting...\n')
                break

            # Save the network model for testing every so often
            if curr_ep == self.checkpoint:
                self.save_model(curr_ep)
                self.checkpoint += self.checkpoint_delta

            # After training, tell workers to start another episode
            for idx in self.worker_tasks:
                worker_queue = self.sync_queues[idx]
                self.sess.run(worker_queue.enqueue(Status.WORKER_START))


class DaggerWorker(object):
    def __init__(self, cluster, serv, worker_idx,
                 num_hosts, num_flows, env, in_charge, ports):

        # Distributed tensorflow and logging related
        self.cluster = cluster
        self.worker_idx = worker_idx
        self.leader_device = '/job:ps/task:0'
        self.worker_device = '/job:worker/task:%d' % worker_idx
        self.num_hosts = num_hosts
        self.num_flows = num_flows

        # Worker in charge is the flow on a given host which can enqueue data,
        # reset and cleanup the environment.
        self.in_charge = in_charge

        # Buffers and parameters required to train
        if self.in_charge:
            self.state_buf = []
            self.action_buf = []

        # Environment's functions only used by in_charge=True.
        self.env = env if in_charge else None
        self.sender = None
        self.expert = TrueDaggerExpert(env)
        self.ports_queue = ports

        self.curr_ep = 0
        self.state_dim = env.state_dim
        self.action_cnt = env.action_cnt

        self.aug_state_dim = self.state_dim + self.action_cnt
        self.prev_action = self.action_cnt - 1

        # Set up Tensorflow for synchronization, training
        self.setup_tf_ops()
        self.sess = tf.Session(
            serv.target, config=tf.ConfigProto(allow_soft_placement=True))
        self.sess.run(tf.global_variables_initializer())

    def cleanup(self):
        if self.sender:
            self.sender.cleanup()
        self.sess.run(self.sync_q.enqueue(Status.WORKER_DONE))

    def setup_tf_ops(self):
        """ Sets up the shared Tensorflow operators and structures
        Refer to DaggerLeader for more information
        """

        # Set up the shared global network and local network.
        with tf.device(self.leader_device):
            with tf.variable_scope('global_cpu'):
                self.global_network_cpu = DaggerLSTM(
                    state_dim=self.aug_state_dim, action_cnt=self.action_cnt)

        with tf.device(self.worker_device):
            with tf.variable_scope('local'):
                self.local_network = DaggerLSTM(
                    state_dim=self.aug_state_dim, action_cnt=self.action_cnt)

        self.init_state = self.local_network.zero_init_state(1)
        self.lstm_state = self.init_state

        # Build shared queues for training data and synchronization
        self.train_q = tf.FIFOQueue(
                self.num_hosts, [tf.float32, tf.int32],
                shared_name='training_feed')

        self.sync_q = tf.FIFOQueue(3, [tf.int16],
                shared_name=('sync_q_%d' % self.worker_idx))

        # Training data is [[aug_state]], [action]
        self.state_data = tf.placeholder(
                tf.float32, shape=(None, self.aug_state_dim))
        self.action_data = tf.placeholder(tf.int32, shape=(None))
        self.enqueue_train_op = self.train_q.enqueue(
                [self.state_data, self.action_data])

        # Sync local network to global network (CPU)
        local_vars = self.local_network.trainable_vars
        global_vars = self.global_network_cpu.trainable_vars
        self.sync_op = tf.group(*[v1.assign(v2) for v1, v2 in zip(
            local_vars, global_vars)])

    def sample_action(self, state):
        """ Given a state buffer in the past step, returns an action
        to perform.

        Appends to the state/action buffers the state and the
        "correct" action to take according to the expert.
        """
        cwnd = state[self.state_dim - 1]
        expert_action = self.expert.sample_action(cwnd)

        # For decision-making, normalize.
        norm_state = normalize(state)

        one_hot_action = one_hot(self.prev_action, self.action_cnt)
        aug_state = norm_state + one_hot_action

        # Fill in state_buf, action_buf
        if self.in_charge:
            self.state_buf.append(aug_state)
            self.action_buf.append(expert_action)

        # Always use the expert on the first episode to get our bearings.
        if self.curr_ep == 0:
            self.prev_action = expert_action
            return expert_action

        # Get probability of each action from the local network.
        pi = self.local_network
        feed_dict = {
            pi.input: [[aug_state]],
            pi.state_in: self.lstm_state,
        }
        ops_to_run = [pi.action_probs, pi.state_out]
        action_probs, self.lstm_state = self.sess.run(ops_to_run, feed_dict)

        # Choose an action to take and update current LSTM state
        action = np.argmax(np.random.multinomial(1, action_probs[0][0] - 1e-5))
        self.prev_action = action

        return action

    def reset_sender(self):
        if self.sender:
            self.sender.cleanup()
            self.sender = None
        port = get_open_udp_port()
        self.sender = Sender(port, train=True)
        self.sender.set_sample_action(self.sample_action)

        if not self.in_charge and self.ports_queue is not None:
            self.ports_queue.put(port)

    def rollout(self):
        """ Start an episode/flow with an empty dataset/environment. """
        self.prev_action = self.action_cnt - 1
        self.lstm_state = self.init_state
        self.reset_sender()

        if self.in_charge:
            # For worker getting data, reset buffers and get all flows' ports
            self.state_buf = []
            self.action_buf = []
            ports = [self.sender.port]
            if self.ports_queue:
                while len(ports) < self.num_flows:
                    ports.append(self.ports_queue.get(block=True, timeout=30))
            self.env.reset(ports)

        self.sender.handshake()
        self.sender.run()

    def run(self, debug=False):
        """Runs for max_ep episodes, each time sending data to the leader."""

        pi = self.local_network
        while True:
            if debug:
                sys.stderr.write('[WORKER %d Ep %d] Starting...\n' %
                                 (self.worker_idx, self.curr_ep))

                global_vars = self.global_network_cpu.trainable_vars[0][0][0]
                global_vars = self.sess.run(global_vars)
                local_vars = self.local_network.trainable_vars[0][0][0]
                local_vars = self.sess.run(local_vars)

                print '[WORKER %d] Before sync' % self.worker_idx
                print '[WORKER %d] global_vars' % self.worker_idx, global_vars
                print '[WORKER %d] local_vars' % self.worker_idx, local_vars
                sys.stdout.flush()

            # Reset local parameters to global
            self.sess.run(self.sync_op)

            if debug:
                local_vars = self.local_network.trainable_vars[0][0][0]
                local_vars = self.sess.run(local_vars)
                print 'After sync'
                print '[WORKER %d] local_vars' % self.worker_idx, local_vars
                sys.stdout.flush()

            # Start a single episode, populating state-action buffers.
            self.rollout()

            if debug and self.in_charge:
                queue_size = self.sess.run(self.train_q.size())
                sys.stderr.write(
                    '[WORKER %d Ep %d]: enqueueing a sequence of data '
                    'into queue of size %d\n' %
                    (self.worker_idx, self.curr_ep, queue_size))

            if self.in_charge:
                # Enqueue a sequence of data into the training queue.
                self.sess.run(self.enqueue_train_op, feed_dict={
                    self.state_data: self.state_buf,
                    self.action_data: self.action_buf})

            self.sess.run(self.sync_q.enqueue(Status.EP_DONE))

            if debug and self.in_charge:
                queue_size = self.sess.run(self.train_q.size())
                sys.stderr.write(
                    '[WORKER %d Ep %d]: finished queueing data. '
                    'queue size now %d\n' %
                    (self.worker_idx, self.curr_ep, queue_size))

            if debug:
                sys.stderr.write('[WORKER %d Ep %d]: waiting for server\n' %
                                 (self.worker_idx, self.curr_ep))

            # Let the leader dequeue EP_DONE
            time.sleep(0.5)

            # Wait until pserver finishes training by blocking on sync_q
            # Only proceeds when it finds a message from the pserver.
            msg = self.sess.run(self.sync_q.dequeue())
            while (msg != Status.WORKER_START and msg != Status.PS_DONE):
                self.sess.run(self.sync_q.enqueue(msg))
                time.sleep(0.5)
                msg = self.sess.run(self.sync_q.dequeue())

            if msg == Status.PS_DONE:
                break

            self.curr_ep += 1
