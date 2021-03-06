import os
import tensorflow as tf
import yaml
import numpy as np
from AlphaZero.network.model import Model
from AlphaZero.network.util import average_gradients, batch_split

os.environ['TF_CPP_MIN_LOG_LEVEL'] = "3"
config = os.path.join(os.path.dirname(__file__), '..',
                      "config", "reinforce.yaml")


class Network(object):

    """
    This module defines the network structure and its operations.

    Args:
        game_config: the rules and size of the game
        train_config: defines the size of the network and configurations in model training.
        num_gpu: the number of GPUs used for computation.
        load_pretrained: whether to load the pre-trained model
        data_format: input format, either "NCHW" or "NHWC". "NCHW" achieves higher performance on GPU, but it's not compatible with CPU.
        cluster, job: for distributed training.
    """

    def __init__(self, game_config, num_gpu=1, train_config=config, load_pretrained=False, data_format="NHWC",
                 cluster=tf.train.ClusterSpec({'main': ['localhost:3333']}), job='main'):
        with open(train_config, "r") as fh:
            self._train_config = yaml.load(fh)
        self._num_gpu = num_gpu
        self._game_config = game_config
        self._data_format = data_format

        self.global_step = tf.get_variable("global_step", [], dtype=tf.int32,
                                           trainable=False, initializer=tf.constant_initializer(0))
        learning_scheme = self._train_config["learning_rate"]
        boundaries = sorted([int(value) for value in learning_scheme.keys()])
        values = [float(learning_scheme[value]) for value in boundaries]
        sess_config = tf.ConfigProto(allow_soft_placement=True)
        sess_config.gpu_options.allow_growth = True
        sess_config.graph_options.optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1
        server = tf.train.Server(
            cluster, job_name=job, task_index=0, config=sess_config)
        self.sess = tf.Session(target=server.target)

        with tf.device('/job:' + job + '/task:0'):
            self.models = []
            self.lr = tf.train.piecewise_constant(
                self.global_step, boundaries[1:], values)
            self.opt = tf.train.MomentumOptimizer(
                self.lr, momentum=self._train_config["momentum"])
            loss_list = []
            grad_list = []
            p_list = []
            v_list = []
            for idx in range(self._num_gpu):
                reuse = bool(idx != 0)
                with tf.variable_scope("model", reuse=reuse):
                    with tf.name_scope("model_{}".format(idx)) as name_scope, tf.device(
                            tf.train.replica_device_setter(worker_device="/gpu:{}".format(idx), ps_device='/cpu:0', ps_tasks=1)):
                        model = Model(
                            self._game_config, self._train_config, data_format=self._data_format)
                        loss = model.get_loss()
                        grad = self.opt.compute_gradients(
                            loss, colocate_gradients_with_ops=True)
                        self.models.append(model)
                        if idx == 0:
                            update_ops = tf.get_collection(
                                tf.GraphKeys.UPDATE_OPS, name_scope)
                loss_list.append(loss)
                grad_list.append(grad)
                p_list.append(model.R_p)
                v_list.append(model.R_v)
            self.loss = tf.add_n(loss_list) / len(loss_list)
            self.grad = average_gradients(grad_list)
            train_op = [self.opt.apply_gradients(
                self.grad, global_step=self.global_step)]
            train_op.extend(update_ops)
            self.train_op = tf.group(*train_op)
            self.R_p = tf.concat(p_list, axis=0)
            self.R_v = tf.concat(v_list, axis=0)
            self.saver = tf.train.Saver(max_to_keep=20)
            self.sess.run(tf.global_variables_initializer())
            if load_pretrained:
                self.saver.restore(
                    self.sess, tf.train.latest_checkpoint(self._train_config['save_dir']))

    def update(self, data):
        """
        Update the model parameters.

        Args:
            data: tuple `(state, action, result, )`. `state` is a numpy array of shape `[None, filters, board_height, board_width]`.
                `action` is a numpy array of shape `[None, flat_move_output]`. `result` is a numpy array of shape `[None]`.

        Returns:
            Average loss of the minibatch.
        """

        feed_dict = {}
        for idx, (model, batch) in enumerate(zip(self.models, batch_split(self._num_gpu, *data))):
            feed_dict[model.x] = batch[0]
            feed_dict[model.p] = batch[1]
            feed_dict[model.v] = batch[2]
            feed_dict[model.is_train] = True
        loss, train_op = self.sess.run(
            [self.loss, self.train_op], feed_dict=feed_dict)
        return loss

    def response(self, data):
        """
        Predict the action and result given current state.

        Args:
            data: `(state, )`. `state` is a numpy array of shape `[None, filters, board_height, board_width]`.

        Returns:
            A tuple `(R_p, R_v)`. `R_p` is the probability distribution of action, a numpy array of shape `[None, 362]`.
            `R_v` is the expected value of current state, a numpy array of shape `[None]`.
        """
        feed_dict = {}
        for idx, (model, batch) in enumerate(zip(self.models, batch_split(self._num_gpu, *data))):
            feed_dict[model.x] = batch[0]
            feed_dict[model.is_train] = False
        R_p, R_v = self.sess.run([self.R_p, self.R_v], feed_dict=feed_dict)
        return R_p, R_v

    def evaluate(self, data):
        """
        Calculate loss and result based on supervised data.

        Args:
            data: tuple `(state, action, result, )`. `state` is a numpy array of shape `[None, filters, board_height, board_width]`.
                `action` is a numpy array of shape `[None, flat_move_output]`. `result` is a numpy array of shape `[None]`.

        Returns:
            A tuple `(loss, acc, mse)`. `loss` is the average loss of the minibatch. `acc` is the position prediction accuracy.
            `mse` is the mean squared error of game outcome.
        """
        feed_dict = {}
        state, action, result = data
        for idx, (model, batch) in enumerate(zip(self.models, batch_split(self._num_gpu, *data))):
            feed_dict[model.x] = batch[0]
            feed_dict[model.p] = batch[1]
            feed_dict[model.v] = batch[2]
            feed_dict[model.is_train] = False
        loss, R_p, R_v = self.sess.run(
            [self.loss, self.R_p, self.R_v], feed_dict=feed_dict)
        mask = np.argmax(R_p, axis=1) == np.argmax(action, axis=1)
        acc = np.mean(mask.astype(np.float32))
        mse = np.mean(np.square(result - R_v)) / 4.
        return loss, acc, mse

    def get_global_step(self):
        """
        Get global step.
        """
        return self.sess.run(self.global_step)

    def save(self, filename):
        """
        Save the model.

        Args:
            filename: prefix to the saved file. The final name is `filename + global_step`
        """
        self.saver.save(self.sess, filename,
                        global_step=self.get_global_step())

    def load(self, filename):
        """
        Load the model.

        Args:
            filename: the name of saved file.
        """
        self.saver.restore(self.sess, filename)
