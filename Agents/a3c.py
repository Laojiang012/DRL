from __future__ import print_function
from collections import namedtuple
import numpy as np
import tensorflow as tf
from model import LSTMPolicy_alpha, LSTMPolicy_beta, LSTMPolicy_gamma
import six.moves.queue as queue
import scipy.signal
import threading
Batch = namedtuple("Batch", ["si", "a", "adv", "r", "terminal", "features_h0", "features_h1", "features_h2", "features"])

# BETA True : use the three layers of LSTM with energy regularization
# BETA False : use the simple one layer of LSTM without energy regularization
BETA = True
GAMMA = False

############################################################################################
def discount(x, gamma):
	# Given a reward signal x = [x_0, x_1 ... x_n]. Calculate the discounted reward for each time step.
	# Thus the result is discounted reward signal [G_0, G_1 ... G_n]
	# G_t= x_t + gamma * x_t+1 + gamma^2 * x_t+2 + ... + gamma^(n-t) * x_n 
    return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

def process_rollout(rollout, gamma, lambda_=1.0):
    """given a rollout, compute its returns and the advantage"""
    # Normally, each field of rollout is a list with a dimension not greater than 20
    # batch_si : a batch of states
    batch_si = np.asarray(rollout.states)
    # batch_a : a batch of actions
    batch_a = np.asarray(rollout.actions)
    # rewards : a signal of rewards
    rewards = np.asarray(rollout.rewards)
    # vpred_t : a signal of V state values
    vpred_t = np.asarray(rollout.values + [rollout.r])
	
	# rewards_plus_v : signal of rewards plus V(s') in the end
    rewards_plus_v = np.asarray(rollout.rewards + [rollout.r])
    # batch_r : calculate the discounted reward for each timestep. A signal of G_t
    batch_r = discount(rewards_plus_v, gamma)[:-1]
    
    delta_t = rewards + gamma * vpred_t[1:] - vpred_t[:-1]
    # this formula for the advantage comes "Generalized Advantage Estimation":
    # https://arxiv.org/abs/1506.02438
    batch_adv = discount(delta_t, gamma * lambda_)
    
    if BETA or GAMMA:
        features_h0 = np.asarray([x[1][0] for x in rollout.features])
        features_h1 = np.asarray([x[1][1] for x in rollout.features])
        features_h2 = np.asarray([x[1][2] for x in rollout.features])
    else:
        features_h0 = features_h1 = features_h2 = []
        
    features = rollout.features[0]
    return Batch(batch_si, batch_a, batch_adv, batch_r, rollout.terminal, features_h0, features_h1, features_h2, features)

############################################################################################
class PartialRollout(object):
    """a piece of a complete rollout.  We run our agent, and process its experience
	once it has processed enough steps."""
    def __init__(self):
        self.states = []
        self.actions = []
        self.rewards = []
        self.values = []
        # r means the state value V(s') where s' is the posteriori state
        # As G = r + gamma * V. So V can be regarded as a pseudo reward here.
        # Anyway V may be a better name for r
        self.r = 0.0
        self.terminal = False
        self.features = []

    def add(self, state, action, reward, value, terminal, features):
    	self.states += [state]
    	self.actions += [action]
    	self.rewards += [reward]
    	self.values += [value]
    	self.terminal = terminal
    	self.features += [features]

    def extend(self, other):
        assert not self.terminal
        self.states.extend(other.states)
        self.actions.extend(other.actions)
        self.rewards.extend(other.rewards)
        self.values.extend(other.values)
        self.r = other.r
        self.terminal = other.terminal
        self.features.extend(other.features)

############################################################################################
class RunnerThread(threading.Thread):
    """One of the key distinctions between a normal environment and a universe environment
	is that a universe environment is _real time_.  This means that there should be a thread
	that would constantly interact with the environment and tell it what to do.  This thread is here."""
	
    def __init__(self, env, policy, num_local_steps, visualise):
        threading.Thread.__init__(self)
        self.queue = queue.Queue(5)
        self.num_local_steps = num_local_steps
        self.env = env
        self.last_features = None
        self.policy = policy
        self.daemon = True
        self.sess = None
        self.summary_writer = None
        self.visualise = visualise

    def start_runner(self, sess, summary_writer):
        self.sess = sess
        self.summary_writer = summary_writer
        self.start()

    def run(self):
        with self.sess.as_default():
            self._run()

    def _run(self):
    	# At last, RunnerThread will execute this part of code
        rollout_provider = env_runner(self.env, self.policy, self.num_local_steps, self.summary_writer, self.visualise)
        while True:
            # the timeout variable exists because apparently, if one worker dies, the other workers
            # won't die with it, unless the timeout is set to some large number.  This is an empirical
            # observation.

            self.queue.put(next(rollout_provider), timeout=600.0)

############################################################################################

def env_runner(env, policy, num_local_steps, summary_writer, render):
    """The logic of the thread runner.  In brief, it constantly keeps on running
	the policy, and as long as the rollout exceeds a certain length, the thread
	runner appends the policy to the queue."""
	# restart the game, last_state is the first frame of pixels with shape [42, 42, 1]
    last_state = env.reset()
    x = last_state[:,:,0]
    last_state = np.stack((x, x, x, x), axis = 2)
    
    # a vector whose every element is zero
    last_features = policy.get_initial_features()
    length = 0
    rewards = 0

    while True:
        terminal_end = False
        # unclear naming. rollout is an instance of the class PartialRollout defined above
        rollout = PartialRollout()

        for _ in range(num_local_steps):
        	# policy represents the local network here. One day I should change these bad names
            fetched = policy.act(last_state, *last_features)
            # action is the action vector in one-hot form. value_ is the V value. features is output hidden states [c, h]
            action, value_, features = fetched[0], fetched[1], fetched[2:]
            # argmax to convert from one-hot. Perform one action
            state, reward, terminal, info = env.step(action.argmax())
            if render:
                env.render()
            state = np.append(last_state[:,:,1:], state, axis = 2)
            # collect the experience
            rollout.add(last_state, action, reward, value_, terminal, last_features)
            length += 1
            rewards += reward

            last_state = state
            last_features = features

            if info:
                summary = tf.Summary()
                for k, v in info.items():
                    summary.value.add(tag=k, simple_value=float(v))
                summary_writer.add_summary(summary, policy.global_step.eval())
                summary_writer.flush()

            timestep_limit = env.spec.tags.get('wrapper_config.TimeLimit.max_episode_steps')
            if terminal or length >= timestep_limit:
                terminal_end = True
                if length >= timestep_limit or not env.metadata.get('semantics.autoreset'):
                    last_state = env.reset()
                    x = last_state[:,:,0]
                    last_state = np.stack((x, x, x, x), axis = 2)
                # After each episode, the author resets the c and h to zeros
                # I may keep the value of c since c has the meaning of memory. And set h to zero since h has the meaning of option.
                #last_features = policy.get_initial_features()
                last_features[1] = policy.get_initial_features()[1]
                #print("Episode finished. Sum of rewards: %d. Length: %d" % (rewards, length))
                length = 0
                rewards = 0
                break

        if not terminal_end:
        	# rollout.r means state value of the posteriori state
            rollout.r = policy.value(last_state, *last_features)

        # once we have enough experience, yield it, and have the ThreadRunner place it on a queue
        # yield creates a generator.
        # More explanations in https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do-in-python
        yield rollout

############################################################################################
class A3C(object):
    def __init__(self, env, task, visualise):
        """An implementation of the A3C algorithm that is reasonably well-tuned for the VNC environments.
		Below, we will have a modest amount of complexity due to the way TensorFlow handles data parallelism.
		But overall, we'll define the model, specify its inputs, and describe how the policy gradients step
		should be computed."""

        self.env = env
        self.task = task
        worker_device = "/job:worker/task:{}/cpu:0".format(task)
        with tf.device(tf.train.replica_device_setter(1, worker_device=worker_device)):
            with tf.variable_scope("global"):
            	# env.observation_space.shape is (42, 42, 1) by default
            	if BETA:
            		self.network = LSTMPolicy_beta(list(env.observation_space.shape[:-1]) + [4], env.action_space.n)
            	elif GAMMA:
            	    self.network = LSTMPolicy_gamma(list(env.observation_space.shape[:-1]) + [4], env.action_space.n)
            	else:
            		self.network = LSTMPolicy_alpha(list(env.observation_space.shape[:-1]) + [4], env.action_space.n)
            	self.global_step = tf.get_variable("global_step", [], tf.int32, initializer=tf.constant_initializer(0, dtype=tf.int32),
                                                   trainable=False)

        with tf.device(worker_device):
            with tf.variable_scope("local"):
            	# pi is a local network instead of the policy function
            	if BETA:
            		self.local_network = pi = LSTMPolicy_beta(list(env.observation_space.shape[:-1]) + [4], env.action_space.n)
            	elif GAMMA:
            	    self.local_network = pi = LSTMPolicy_gamma(list(env.observation_space.shape[:-1]) + [4], env.action_space.n)
            	else:
            		self.local_network = pi = LSTMPolicy_alpha(list(env.observation_space.shape[:-1]) + [4], env.action_space.n)
            	pi.global_step = self.global_step
               
			# ac : action vector
            self.ac = tf.placeholder(tf.float32, [None, env.action_space.n], name="ac")
            # adv : advantage value G_t - V
            self.adv = tf.placeholder(tf.float32, [None], name="adv")
            # r : total discounted reward
            self.r = tf.placeholder(tf.float32, [None], name="r")
			# pi.logits : unnormalised policy(a | s). So log_prob_tf is log policy distribution
            log_prob_tf = tf.nn.log_softmax(pi.logits)
            # prob_tf : normalized policy distribution
            prob_tf = tf.nn.softmax(pi.logits)

            # the "policy gradients" loss:  its derivative is precisely the policy gradient
            # notice that self.ac is a placeholder that is provided externally.
            # adv will contain the advantages, as calculated in process_rollout
            pi_loss = - tf.reduce_sum(tf.reduce_sum(log_prob_tf * self.ac, [1]) * self.adv)

            # loss of value function
            vf_loss = 0.5 * tf.reduce_sum(tf.square(pi.vf - self.r))
            entropy = - tf.reduce_sum(prob_tf * log_prob_tf)
			# bs : batch size
            bs = tf.to_float(tf.shape(pi.x)[0])
            
            # option loss
            if BETA:
                h_loss_0 = tf.square(tf.reduce_sum(tf.square(pi.state_in[1][0])) - tf.reduce_sum(tf.square(pi.state_out[1][0:1])))
                h_loss_1 = tf.square(tf.reduce_sum(tf.square(pi.state_in[1][1])) - tf.reduce_sum(tf.square(pi.state_out[1][1:2])))
                h_loss_2 = tf.square(tf.reduce_sum(tf.square(pi.state_in[1][2])) - tf.reduce_sum(tf.square(pi.state_out[1][2:3])))
                H_loss = 0.001 * h_loss_0 + 0.01 * h_loss_1 + 0.1 * h_loss_2
                # Total Loss function, may tune the lambda value here.
                self.loss = pi_loss + 0.5 * vf_loss - 0.01 * entropy + H_loss
            elif GAMMA:
                h_loss_0 = tf.square(tf.reduce_sum(tf.square(pi.state_in[1][0])) - tf.reduce_sum(tf.square(pi.state_out[1][0])))
                h_loss_1 = tf.square(tf.reduce_sum(tf.square(pi.state_in[1][1])) - tf.reduce_sum(tf.square(pi.state_out[1][1])))
                h_loss_2 = tf.square(tf.reduce_sum(tf.square(pi.state_in[1][2])) - tf.reduce_sum(tf.square(pi.state_out[1][2])))
                H_loss = 0.001 * h_loss_0 + 0.01 * h_loss_1 + 0.1 * h_loss_2
                self.loss = pi_loss + 0.5 * vf_loss - 0.01 * entropy + H_loss            
            else:
                self.loss = pi_loss + 0.5 * vf_loss - 0.01 * entropy

            # 20 represents the number of "local steps":  the number of timesteps
            # we run the policy before we update the parameters.
            # The larger local steps is, the lower is the variance in our policy gradients estimate
            # on the one hand;  but on the other hand, we get less frequent parameter updates, which
            # slows down learning.  In this code, we found that making local steps be much
            # smaller than 20 makes the algorithm more difficult to tune and to get to work.
            
            # RunnerThread is a class. See definition above.
            rollout_size = 20
            self.runner = RunnerThread(env, pi, rollout_size, visualise)

			# Constructs symbolic partial derivatives of self.loss w.r.t. variable in pi.var_list
            grads = tf.gradients(self.loss, pi.var_list)

            if BETA or GAMMA:
            	# summary is about the tensorboard. It exports the information about the model
                #tf.summary.scalar("model/policy_loss", pi_loss / bs)
                #tf.summary.scalar("model/value_loss", vf_loss / bs)
                tf.summary.scalar("model/energy2", tf.reduce_sum(tf.square(pi.state_in[1][2])))
                tf.summary.scalar("model/energy1", tf.reduce_sum(tf.square(pi.state_in[1][1])))
                tf.summary.scalar("model/energy0", tf.reduce_sum(tf.square(pi.state_in[1][0])))
                #tf.summary.scalar("model/entropy", entropy / bs)
                #tf.summary.image("model/state", pi.x)
                #tf.summary.scalar("model/grad_global_norm", tf.global_norm(grads))
                #tf.summary.scalar("model/var_global_norm", tf.global_norm(pi.var_list))
                self.summary_op = tf.summary.merge_all()
            else:
                tf.summary.scalar("model/policy_loss", pi_loss / bs)
                self.summary_op = tf.summary.merge_all()
                
			
			# clipping to avoid the exploding or vanishing gradient values
            grads, _ = tf.clip_by_global_norm(grads, 40.0)

            # copy weights from the parameter server to the local model by tf.assign : update the value of v1 by v2
            # self.network is the global network while pi is the local one
            # tf.group : An Operation that executes all its inputs.
            # tf.assign is a shallow copy
            self.sync = tf.group(*[v1.assign(v2) for v1, v2 in zip(pi.var_list, self.network.var_list)])
            
            # list zip gives [(grad value, variable name), () ... ()]
            # this is a key step to send the local gradient value to the parameter server. 
            # grads is the locally computed gradient value and self.network is the global network in ps
            # actually, we combine the gradient value with the variables of the global network 
            grads_and_vars = list(zip(grads, self.network.var_list))
            
            # inc_step maps to the ops that global step += some value
            inc_step = self.global_step.assign_add(tf.shape(pi.x)[0])

            # each worker has a different set of adam optimizer parameters
            opt = tf.train.AdamOptimizer(1e-4)
            # tf.group's function is simply execute the two ops in the same time in one command
            self.train_op = tf.group(opt.apply_gradients(grads_and_vars), inc_step)
            self.summary_writer = None
            self.local_steps = 0

    def start(self, sess, summary_writer):
        self.runner.start_runner(sess, summary_writer)
        self.summary_writer = summary_writer

    def pull_batch_from_queue(self):
        """self explanatory:  take a rollout from the queue of the thread runner."""
        rollout = self.runner.queue.get(timeout=600.0)
        while not rollout.terminal:
            try:
                rollout.extend(self.runner.queue.get_nowait())
            except queue.Empty:
                break
        return rollout

    def process(self, sess):
        """process grabs a rollout that's been produced by the thread runner,
		and updates the parameters. The update is then sent to the parameter
		server."""
		
        size = self.local_network.size
        sess.run(self.sync)  
        rollout = self.pull_batch_from_queue()
        batch = process_rollout(rollout, gamma=0.99, lambda_=1.0)

        should_compute_summary = self.task == 0 and self.local_steps % 11 == 0

        if should_compute_summary:
            fetches = [self.summary_op, self.train_op, self.global_step]
        else:
            fetches = [self.train_op, self.global_step]
            
        if BETA:
        	feed_dict = {
    		self.local_network.x: batch.si,
	        self.ac: batch.a,
	        self.adv: batch.adv,
	        self.r: batch.r,
	        self.local_network.state_in[0][0]: batch.features[0][0:1],
	        self.local_network.state_in[0][1]: batch.features[0][1:2],
	        self.local_network.state_in[0][2]: batch.features[0][2:3],
	        self.local_network.state_in[1][0]: batch.features[1][0:1],
	        self.local_network.state_in[1][1]: batch.features[1][1:2],
	        self.local_network.state_in[1][2]: batch.features[1][2:3]
	        }
        elif GAMMA:
	        feed_dict = {
    		self.local_network.x: batch.si,
	        self.ac: batch.a,
	        self.adv: batch.adv,
	        self.r: batch.r,
	        self.local_network.state_in[0][0]: batch.features[0][0],
	        self.local_network.state_in[0][1]: batch.features[0][1],
	        self.local_network.state_in[0][2]: batch.features[0][2],
	        self.local_network.state_in[1][0]: batch.features[1][0],
	        self.local_network.state_in[1][1]: batch.features[1][1],
	        self.local_network.state_in[1][2]: batch.features[1][2],
	        self.local_network.h_aux0: np.reshape(batch.features_h0, [-1, size]),
	        self.local_network.h_aux1: np.reshape(batch.features_h1, [-1, size]),
	        self.local_network.h_aux2: np.reshape(batch.features_h2, [-1, size])	        
	        }
        else:
        	feed_dict = {
        	self.local_network.x: batch.si,
        	self.ac: batch.a,
        	self.adv: batch.adv,
        	self.r: batch.r,
        	self.local_network.state_in[0]: batch.features[0],
        	self.local_network.state_in[1]: batch.features[1]}
        	
        fetched = sess.run(fetches, feed_dict=feed_dict)
        if should_compute_summary:
        	self.summary_writer.add_summary(tf.Summary.FromString(fetched[0]), fetched[-1])
        	self.summary_writer.flush()
        self.local_steps += 1
        
