import numpy as np
import tensorflow as tf
import gpflow
import pandas as pd
import copy

from .mgpr import MGPR
from .smgpr import SMGPR
from .. import controllers
from .. import rewards

float_type = gpflow.settings.dtypes.float_type


class PILCO(gpflow.models.Model):
    def __init__(self, X, Y, num_induced_points=None, horizon=30, controller=None,
                reward=None, m_init=None, S_init=None, name=None):
        super(PILCO, self).__init__(name)
        if not num_induced_points:
            self.mgpr = MGPR(X, Y)
        else:
            self.mgpr = SMGPR(X, Y, num_induced_points)
        self.state_dim = Y.shape[1]
        self.control_dim = X.shape[1] - Y.shape[1]
        self.horizon = horizon

        if controller is None:
            self.controller = controllers.LinearController(self.state_dim, self.control_dim)
        else:
            self.controller = controller

        if reward is None:
            self.reward = rewards.ExponentialReward(self.state_dim)
        else:
            self.reward = reward

        if m_init is None or S_init is None:
            # If the user has not provided an initial state for the rollouts,
            # then define it as the first state in the dataset.
            self.m_init = X[0:1, 0:self.state_dim]
            self.S_init = np.diag(np.ones(self.state_dim) * 0.1)
        else:
            self.m_init = m_init
            self.S_init = S_init
        self.optimizer = None

    @gpflow.name_scope('likelihood')
    def _build_likelihood(self):
        # This is for tuning controller's parameters
        reward = self.predict(self.m_init, self.S_init, self.horizon)[2]
        return reward

    def optimize(self):
        self.optimize_models()
        self.optimize_policy()

    def optimize_models(self):
        '''
        Optimize GP models
        '''
        import time
        start = time.time()
        self.mgpr.optimize()
        end = time.time()
        print("Finished with GPs' optimization in %.1f seconds" % (end - start))

        lengthscales = {}; variances = {}; noises = {};
        i = 0
        for model in self.mgpr.models:
            lengthscales['GP' + str(i)] = model.kern.lengthscales.value
            variances['GP' + str(i)] = np.array([model.kern.variance.value])
            noises['GP' + str(i)] = np.array([model.likelihood.variance.value])
            i += 1
        print('-----Learned models------')
        pd.set_option('precision', 3)
        print('---Lengthscales---')
        print(pd.DataFrame(data=lengthscales))
        print('---Variances---')
        print(pd.DataFrame(data=variances))
        print('---Noises---')
        print(pd.DataFrame(data=noises))

    def optimize_policy(self, maxiter=50):
        '''
        Optimize controller's parameter's
        '''
        import time
        start = time.time()
        if self.optimizer:
            self.optimizer._optimizer.minimize(session=self.optimizer._model.enquire_session(None),
                           feed_dict=self.optimizer._gen_feed_dict(self.optimizer._model, None),
                           step_callback=None)
        else:
            self.optimizer = gpflow.train.ScipyOptimizer(method="L-BFGS-B")
            self.optimizer.minimize(self, disp=True, maxiter=maxiter)
        end = time.time()
        print("Finished with Controller's optimization in %.1f seconds" % (end - start))

    @gpflow.autoflow((float_type,[None, None]))
    def compute_action(self, x_m):
        return self.controller.compute_action(x_m, tf.zeros([self.state_dim, self.state_dim], float_type))[0]

    def predict(self, m_x, s_x, n):
        loop_vars = [
            tf.constant(0, tf.int32),
            m_x,
            s_x,
            tf.constant([[0]], float_type)
        ]

        _, m_x, s_x, reward = tf.while_loop(
            # Termination condition
            lambda j, m_x, s_x, reward: j < n,
            # Body function
            lambda j, m_x, s_x, reward: (
                j + 1,
                *self.propagate(m_x, s_x),
                tf.add(reward, self.reward.compute_reward(m_x, s_x)[0])
            ), loop_vars
        )

        return m_x, s_x, reward

    def propagate(self, m_x, s_x):
        m_u, s_u, c_xu = self.controller.compute_action(m_x, s_x)

        m = tf.concat([m_x, m_u], axis=1)
        s1 = tf.concat([s_x, s_x@c_xu], axis=1)
        s2 = tf.concat([tf.transpose(s_x@c_xu), s_u], axis=1)
        s = tf.concat([s1, s2], axis=0)

        M_dx, S_dx, C_dx = self.mgpr.predict_on_noisy_inputs(m, s)
        M_x = M_dx + m_x
        #TODO: cleanup the following line
        S_x = S_dx + s_x + s1@C_dx + tf.matmul(C_dx, s1, transpose_a=True, transpose_b=True)

        # While-loop requires the shapes of the outputs to be fixed
        M_x.set_shape([1, self.state_dim]); S_x.set_shape([self.state_dim, self.state_dim])
        return M_x, S_x

    def restart_controller(self, restarts=1, verbose=False):
        # Save values
        values = self.read_values()
        old_reward = copy.deepcopy(self.compute_return())

        # Reinitialize values
        self.controller.randomize()

        # Make sure this stayed the same
        if verbose: print(old_reward)
        # Retrain
        self.optimize_policy()

        reward = copy.deepcopy(self.compute_return())
        if verbose:
            print(old_reward)
            print(reward)
        # If restart successgul keep new values, otherwise return to the previous
        if old_reward > reward:
            if verbose: print("Restoring controller values")
            self.assign(values)
            if verbose: print(self.compute_return())
        else:
            if verbose: print('Successful restart')
            values = self.read_values()
            old_reward = reward

    @gpflow.autoflow()
    def compute_return(self):
        return self._build_likelihood()
