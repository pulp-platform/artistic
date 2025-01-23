# Copyright 2025 ETH Zurich and University of Bologna.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
#
# Thomas Benz <tbenz@iis.ee.ethz.ch>
# Paul Scheffler <paulsc@iis.ee.ethz.ch>
# Nils Wistoff <nwistoff@iis.ee.ethz.ch>
# Philippe Sauter <phsauter@iis.ee.ethz.ch>

"""Exploratory script to generate alpha transparency scales"""

import numpy
import math
from lmfit import minimize, Parameters


alpha_vals = [1, 0.0, 0.0, 0.0]
alpha_idxs = [0, 27, 27, 27]


def fcn2min(params, x, data):
    adj1 = params['adj1'].value
    adj2 = params['adj2'].value
    adj3 = params['adj3'].value
    adj4 = params['adj4'].value

    model = adj1 + numpy.power(adj3 + adj2 * numpy.array(x), adj4)
    return model - data


params = Parameters()
params.add('adj1', value=1)
params.add('adj2', value=1)
params.add('adj3', value=1)
params.add('adj4', value=math.e, min=math.e-0.1, max=math.e+0.1)

result = minimize(fcn2min, params, args=(alpha_idxs, alpha_vals))

adj1 = result.params['adj1'].value
adj2 = result.params['adj2'].value
adj3 = result.params['adj3'].value
adj4 = result.params['adj4'].value

for x in numpy.linspace(0, 27, 28):
    print(x, adj1 + numpy.power(adj3 + adj2 * x, adj4))
