from common_definitions import *
from utils.utils import *


class Z_Norm_IntermediateLayer(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()

    def build(self, input_shape):
        channel_size = input_shape[-1]
        # self.mean_lstd = tf.keras.layers.Dense(channel_size * 2, kernel_initializer=KERNEL_INITIALIZER_CLOSE_ZERO, kernel_regularizer=KERNEL_REGULARIZER)
        self.mean_lstd = tf.keras.layers.Conv2D(channel_size * 2, 1, padding="same",
                                                kernel_initializer=KERNEL_INITIALIZER_CLOSE_VALUE(0))

    def call(self, v1, v2, logdet=False, reverse=False):
        """
        to sample forward: norm v1 with mean and lvar learned from v2
        :param v1:
        :param v2:
        :return:
        """
        mean_lstd = self.mean_lstd(v2)
        mean, lstd = split_last_channel(mean_lstd)
        std = tf.exp(lstd)

        if reverse:
            output = v1 * std * TEMP + mean
        else:
            output = (v1 - mean) / std

        if logdet:
            return output, tf.reduce_mean(logpz(mean, lstd, v1), 0)
            # return output, tf.reduce_mean(tf.math.reciprocal_no_nan(std), 0)
        else:
            return output, 0.


class Z_Norm_LastLayer(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()

    def build(self, input_shape):
        channel_size = input_shape[-1]
        self.mean_lstd = self.add_weight("Mean_Logvar", (1, input_shape[1], input_shape[2], channel_size * 2,),
                                         initializer=KERNEL_INITIALIZER_CLOSE_VALUE(0), trainable=True)

    def call(self, v1, logdet=False, reverse=False):
        """
        to sample forward: norm v1 with mean and lvar learned from v2
        :param v1:
        :param v2:
        :return:
        """
        mean_lstd = self.mean_lstd
        mean, lstd = split_last_channel(mean_lstd)
        std = tf.exp(lstd)

        if reverse:
            output = v1 * std * TEMP + mean
        else:
            output = (v1 - mean) / std

        if logdet:
            return output, tf.reduce_mean(logpz(mean, lstd, v1), 0)
            # return output, tf.reduce_mean(tf.math.reciprocal_no_nan(std), 0)
        else:
            return output, 0.


class ActNormalization(tf.keras.layers.Layer):
    def __init__(self, output_only_one=False):
        super().__init__()

        # temp var
        self._initiated = False  # toggle var to initiate the value
        self.output_only_one = output_only_one

    def build(self, input_shape):
        self.channel_size = input_shape[-1]
        self.s = self.add_weight("s", shape=(1, 1, 1, self.channel_size), initializer=tf.keras.initializers.ones(),
                                 trainable=True)
        self.b = self.add_weight("b", shape=(1, 1, 1, self.channel_size), initializer=tf.keras.initializers.zeros(),
                                 trainable=True)

    def call(self, inputs, logdet=False, reverse=False):
        if (not self._initiated) and (not reverse):
            std = tf.math.reduce_std(inputs, [0, 1, 2])
            mean = tf.math.reduce_mean(inputs, [0, 1, 2])
            self.s.assign(1 / std[None, None, None, :])
            self.b.assign(-mean / std[None, None, None, :])

            self._initiated = True  # change the toggle var

        if reverse:
            x = (inputs - self.b) / self.s
        else:
            x = self.s * inputs + self.b

        if logdet:
            return x, inputs.shape[1] * inputs.shape[2] * tf.reduce_sum(log_abs(self.s))
        else:
            if self.output_only_one:
                return x
            else:
                return x, 0.


class InvConv1(tf.keras.layers.Layer):
    """
    This is replacement of fixed permutation
    The weight has to be guaranteed to be square-sized, no bias
    """

    def __init__(self):
        super().__init__()

    def build(self, input_shape):
        channel_size = input_shape[-1]
        self.W = self.add_weight("W_1_1", shape=(channel_size, channel_size),
                                 initializer=tf.keras.initializers.Orthogonal(),
                                 trainable=True)
        self.channel_size = channel_size

    def call(self, inputs, logdet=False, reverse=False):
        if logdet:
            if tf.linalg.det(self.W) == 0:
                W = self.W + KERNEL_INITIALIZER_CLOSE_VALUE(0)(
                    shape=self.W.shape)  # doing this will move the matrix to invertible location
            else:
                W = self.W
        else:
            W = self.W

        W = tf.reshape(tf.linalg.inv(W) if reverse else W, [1, 1, self.channel_size, self.channel_size])

        x = tf.nn.conv2d(inputs, W, [1, 1, 1, 1], padding="SAME")

        if logdet:
            return x, inputs.shape[1] * inputs.shape[2] * tf.squeeze(
                tf.math.log(tf.math.abs(tf.linalg.det(W)) + TF_EPS))
        else:
            return x, 0.


class AffineCouplingLayer(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()

    def build(self, input_shape):
        channel_size = input_shape[-1]
        self.channel_size = channel_size

        self.nn = self.nnLayer(channel_size)

    def nnLayer(self, channel_size):
        inputs = tf.keras.Input(shape=(None, None, channel_size // 2))

        x = tf.keras.layers.Conv2D(32, 4, activation=ACTIVATION, kernel_initializer=KERNEL_INITIALIZER, padding="same")(
            inputs)
        x = ActNormalization(output_only_one=True)(x)
        x = tf.keras.layers.Dropout(DROPOUT_N)(x)
        x = tf.keras.layers.Conv2D(32, 1, activation=ACTIVATION, kernel_initializer=KERNEL_INITIALIZER, padding="same")(
            x)
        x = ActNormalization(output_only_one=True)(x)
        x = tf.keras.layers.Dropout(DROPOUT_N)(x)

        s = tf.keras.layers.Conv2D(channel_size // 2, 4, kernel_initializer=KERNEL_INITIALIZER_CLOSE_VALUE(2.),
                                   padding="same")(x)
        t = tf.keras.layers.Conv2D(channel_size // 2, 4, kernel_initializer=KERNEL_INITIALIZER_CLOSE_VALUE(0.),
                                   padding="same")(x)

        # postprocess s & t
        s = tf.nn.sigmoid(s)
        t = tf.nn.sigmoid(t)

        return tf.keras.Model(inputs, [s, t])

    def forward_block(self, x, s, t):
        y = x * s + t
        return y

    def backward_block(self, y, s, t):
        x = y / s - t / s
        return x

    def call(self, inputs, logdet=False, reverse=False, training=False):
        if reverse:
            v1, v2 = split_last_channel(inputs)
            s2, t2 = self.nn(v2, training=training)
            u1 = self.backward_block(v1, s2, t2)

            # change convention for variable purpose
            v1 = u1
        else:
            u1, u2 = split_last_channel(inputs)
            s2, t2 = self.nn(u2, training=training)
            v1 = self.forward_block(u1, s2, t2)
            v2 = u2

        if logdet:
            _logabsdet = tf.reduce_mean(tf.reduce_sum(log_abs(s2), [1, 2, 3]), 0)
            return (v1, v2), _logabsdet
        else:
            return (v1, v2), 0.


class FlowStep(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()
        self.an = ActNormalization()
        self.perm = InvConv1()
        self.acl = AffineCouplingLayer()

    def call(self, inputs, logdet=False, reverse=False, training=False):
        if not reverse:
            # act norm
            x, logdet_an = self.an(inputs, logdet, reverse)

            # invertible 1x1 layer
            x, logdet_perm = self.perm(x, logdet, reverse)

            # affine coupling layer
            x, logdet_acl = self.acl(x, logdet, reverse, training)
        else:
            # affine coupling layer
            x, _ = self.acl(inputs, logdet, reverse, training)
            x = tf.concat(x, axis=-1)  # concat the two output produced

            # invertible 1x1 layer
            x, _ = self.perm(x, logdet, reverse)

            # act norm
            x, _ = self.an(x, logdet, reverse)

        if logdet:
            # print(logdet_an, logdet_perm, logdet_acl)
            return x, logdet_an + logdet_perm + logdet_acl
        else:
            return x, 0.


class SqueezeLayer(tf.keras.layers.Layer):
    def __init__(self, factor_size):
        super().__init__()
        self.factor_size = factor_size

    def build(self, input_shape):
        self._input_shape = input_shape

    def call(self, inputs, reverse=False, target_width=None):
        if reverse:
            return unsqueeze2d(inputs, self.factor_size)
        else:
            return squeeze2d(inputs, self.factor_size)


class GLOW(tf.keras.Model):
    def __init__(self, factor_size, K, L, window_length, channel_size):
        super().__init__()

        # variables
        self.channel_size = channel_size

        # layers
        self.squeezelayers = [SqueezeLayer(factor_size) for _ in range(L)]
        self.flowsteps = [[FlowStep() for _ in range(K)] for _ in range(L)]
        self.logpzlayers = [Z_Norm_IntermediateLayer() for _ in range(L - 1)]
        self.logpzlayers_last = Z_Norm_LastLayer()

        # constant var
        self.factor_size = factor_size
        self.K = K
        self.L = L
        self.window_length = window_length

    def call(self, inputs, logdet=False, reverse=False, training=False):
        inputs = tf.cast(inputs, dtype=tf.float32)  # cast it

        if not reverse:
            x = inputs

            # run inner iteration of L-1 times
            z = []
            logdet_fs_total = 0

            # Step 2.
            for i_l in range(self.L):
                # Step 2.1
                x = self.squeezelayers[i_l](x, reverse)

                # Step 2.2 run flow step for K times
                for i_k in range(self.K - 1):
                    (ya, yb), logdet_fs = self.flowsteps[i_l][i_k](x, logdet, reverse, training)
                    x = concat_last_channel(ya, yb)  # flip the ya and yb as of the architecture design
                    if logdet: logdet_fs_total += logdet_fs

                # Step 2.3 run the last K without concat
                (ya, yb), logdet_fs = self.flowsteps[i_l][self.K - 1](x, logdet, reverse, training)
                x = yb

                if i_l == self.L - 1:
                    x = concat_last_channel(ya, yb)
                    # logpz with the mean and var accordingly
                    ya, logpz = self.logpzlayers_last(x, logdet, reverse)
                    if logdet: logdet_fs_total += logdet_fs + logpz
                else:
                    # logpz with the mean and var accordingly
                    ya, logpz = self.logpzlayers[i_l](ya, yb, logdet, reverse)
                    if logdet: logdet_fs_total += logdet_fs + logpz

                # logpz with the mean and var accordingly
                ya = tf.compat.v1.layers.flatten(ya)

                # Step 2.4 append to the z
                z.append(ya)

            z_total = tf.concat(z, axis=-1)
            if logdet:
                return z_total, tf.squeeze(
                    logdet_fs_total / tf.math.log(2.))  # divide by all pixel... this is now in bits/dim
            else:
                return z_total, 0.
        else:
            assert not logdet  # inv cant have logdet
            z_total = inputs
            z_sizes = [self.window_length // 2 ** (i_l + 1) * self.channel_size for i_l in
                       range(self.L)]  # the sizes as effect to the multi-scale arch
            x = None

            for i_l, z_size in enumerate(z_sizes[::-1]):
                if i_l == 0:
                    z_size *= 2
                i_l = self.L - i_l - 1  # reverse the index

                z_total, z = split_last_channel(z_total, boundary=-z_size)  # get the z

                channel_order = int(CHANNEL_SIZE * self.factor_size ** (i_l + 1) / 2 ** i_l)
                za_channel_size = channel_order if i_l == self.L - 1 else channel_order // 2
                wh_size = self.window_length // 2 ** (i_l + 1)

                if i_l == self.L - 1:
                    # reverse the renorm last k
                    z, _ = self.logpzlayers_last(tf.reshape(z, [-1, wh_size, 1, za_channel_size]), logdet, reverse)
                else:
                    # reverse the renorm last k
                    z, _ = self.logpzlayers[i_l](tf.reshape(z, [-1, wh_size, 1, za_channel_size]), x, logdet, reverse)

                    z = concat_last_channel(z, x)  # concat the z and previous x

                # run the last K
                x, _ = self.flowsteps[i_l][self.K - 1](z, logdet, reverse)

                # run flow step for K times
                for i_k in reversed(range(self.K - 1)):
                    x, _ = self.flowsteps[i_l][i_k](x, logdet, reverse)

                # unsqueeze
                x = self.squeezelayers[i_l](x, reverse, self.window_length // 2 ** i_l)

            return x, 0.
