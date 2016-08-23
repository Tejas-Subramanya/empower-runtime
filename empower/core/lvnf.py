#!/usr/bin/env python3
#
# Copyright (c) 2016 Roberto Riggio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Light Virtual Network Function."""

import types
import time

from empower.main import RUNTIME

import empower.logger
LOG = empower.logger.get_logger()

# add lvnf message sent, no status received
PROCESS_SPAWNING = "spawning"

# add lvnf message sent, status received (process is running)
PROCESS_RUNNING = "running"

# del lvnf message sent, no status received
PROCESS_STOPPING = "stopping"

# del lvnf message sent, status
PROCESS_STOPPED = "stopped"

# del lvnf message sent, no status received yet
PROCESS_MIGRATING_STOP = "migrating_stop"

# add lvnf message sent, no status received yet
PROCESS_MIGRATING_START = "migrating_start"


class LVNF(object):
    """A Light Virtual Network Function.

    An object representing a Light Virtual Network Function. An LVNF is
    an instance of a Image. Each Image consists of a click script
    implementing the specific VNF. The following boilerplate code is
    automatically generated by the EmPOWER Agent (one port):

        in_0 :: FromHost(vnf-br0-0-0);
        out_0 :: ToHost(vnf-br0-0-0);

    vnf-<bridge>-<k>-<n> is the name of the virtual interface to be created by
    this VNF. <bridge> is the name of the OVS bridge where the VNF is attached,
    <k> is a counter incremented by the agent for each deployed VNF, <n> is
    the virtual port id. Notice how the VNF developer needs not to
    care about the specific value of X and Y, however he/she must use 'in_0'
    and 'out_0' as respectivelly inputs and outputs of his/her VNF. For
    example a valid VNF script for this case is the following:

        in_0 -> null::Null() -> out_0

    After an LVNF is created it is not automatically spawed in a CPP.
    Developers must manually assign the cpp attribute in order to install the
    LVNF in a specific CPP. If the LVNF was previously installed in a CPP,
    then it is first undeployed from the original CPP and then deployed on the
    new CPP. Setting the cpp is asynch, so the fact that the attribute was set
    does not mean that the LVNF was deployed. The developer must either check
    periodically the status of the LVNF or he/she must register a callback
    for the LVNF status event.

    Note, as opposed to LVAPs which can live outside a tenant, an LVNF is
    bound to one and only one tenant. As a result the runtime does not have a
    list of LVNFs which is instead keps by each tenant object.

    Attributes:
        cpp: Pointer to the CPP hosting this LVNF (CPP)
        lvnf_id: The lvnf id (UUID)
        tenant_id: The Tenant id (UUID)
        image: The Image used by this LVNF (Image)
        ports: The virtual ports supported by this LVNF (Map)
        message: The error message retuned by Click (String)
        returncode: The Click process return code, only if stopped (Integer)
        process: The status of the process (running, migrating, migrated,
            stopped, done)
    """

    def __init__(self, lvnf_id, tenant_id, image, cpp):

        self.lvnf_id = lvnf_id
        self.tenant_id = tenant_id
        self.image = image
        self.ports = {}
        self.returncode = None
        self.context = None
        self.__state = None
        self.__cpp = cpp
        self.__target_cpp = None
        self.__migration_timer = None
        self.__creation_timer = None
        self.__chains = []

    def start(self):
        """Spawn LVNF."""

        tenant = RUNTIME.tenants[self.tenant_id]

        if self.lvnf_id in tenant.lvnfs:
            raise KeyError("Already defined %s", self.lvnf_id)

        tenant.lvnfs[self.lvnf_id] = self

        self.state = PROCESS_SPAWNING

    def stop(self):
        """Remove LVNF."""

        self.state = PROCESS_STOPPING

    @property
    def state(self):
        """Return the state."""

        return self.__state

    @state.setter
    def state(self, state):
        """Set the CPP."""

        LOG.info("LVNF %s transition %s->%s", self.lvnf_id, self.state, state)

        if self.state:
            method = "_%s_%s" % (self.state, state)
        else:
            method = "_none_%s" % state

        if hasattr(self, method):
            callback = getattr(self, method)
            callback()
            return

        raise IOError("Invalid transistion %s -> %s" % (self.state, state))

    def _running_spawning(self):

        # set new state
        self.__state = PROCESS_MIGRATING_STOP

        # remove lvnf
        self.cpp.connection.send_del_lvnf(self.lvnf_id)

    def _running_stopping(self):

        # set new state
        self.__state = PROCESS_STOPPING

        # send LVNF del message
        self.cpp.connection.send_del_lvnf(self.lvnf_id)

    def _stopping_stopped(self):

        # set new state
        self.__state = PROCESS_STOPPED

    def _spawning_running(self):

        delta = int((time.time() - self.__creation_timer) * 1000)
        LOG.info("LVNF %s started in %sms", self.lvnf_id, delta)

        self.__state = PROCESS_RUNNING

    def _spawning_stopped(self):

        self.__state = PROCESS_STOPPED

    def _running_migrating_stop(self):

        # set time
        self.__migration_timer = time.time()

        # set new state
        self.__state = PROCESS_MIGRATING_STOP

        # remove lvnf
        self.cpp.connection.send_del_lvnf(self.lvnf_id)

        # look for LVAPs that points to this LVNF
        self.__chains = []

        for lvap in RUNTIME.lvaps.values():
            for out_port in lvap.ports:
                for rule in list(lvap.ports[out_port].next):
                    v_port = lvap.ports[out_port].next[rule]
                    in_port = v_port.virtual_port_id
                    if v_port in self.ports.values():
                        save = (lvap, rule, out_port, in_port)
                        self.__chains.append(save)
                        del lvap.ports[0].next[rule]

    def _migrating_stop_migrating_start(self):

        # set new cpp
        self.cpp = self.__target_cpp

        # set new state
        self.__state = PROCESS_MIGRATING_START

        # add lvnf
        self.cpp.connection.send_add_lvnf(self.image, self.lvnf_id,
                                          self.tenant_id, self.context)

    def _migrating_start_running(self):

        self.__state = PROCESS_RUNNING

        delta = int((time.time() - self.__migration_timer) * 1000)
        LOG.info("LVNF %s migration took %sms", self.lvnf_id, delta)

        LOG.info("Restoring chains")
        for chain in self.__chains:
            vnf = chain[0]
            rule = chain[1]
            out_port = chain[2]
            in_port = chain[3]
            LOG.info("LVAP %s port [%u] next [%s] -> %u", vnf.addr,
                     out_port, rule, in_port)
            vnf.ports[out_port].next[rule] = self.ports[in_port]

        self.__chains = []

    def _none_spawning(self):

        # set timer
        self.__creation_timer = time.time()

        # set new state
        self.__state = PROCESS_SPAWNING

        # send LVNF add message
        self.cpp.connection.send_add_lvnf(self.image,
                                          self.lvnf_id,
                                          self.tenant_id)

    def _none_running(self):

        self.__state = PROCESS_RUNNING

    @property
    def cpp(self):
        """Return the CPP."""

        return self.__cpp

    @cpp.setter
    def cpp(self, cpp):
        """Set the CPP."""

        if self.state == PROCESS_RUNNING:

            # save target cpp
            self.__target_cpp = cpp

            # move to new state
            self.state = PROCESS_MIGRATING_STOP

        elif self.state == PROCESS_MIGRATING_STOP:

            # set cpp
            self.__cpp = cpp
            self.__target_cpp = None

        else:

            IOError("Setting CPP on invalid state: %s" % self.state)

    def to_dict(self):
        """Return a JSON-serializable dictionary representing the Poll."""

        return {'lvnf_id': self.lvnf_id,
                'image': self.image,
                'tenant_id': self.tenant_id,
                'cpp': self.cpp,
                'state': self.state,
                'returncode': self.returncode,
                'ports': self.ports}

    def __eq__(self, other):

        if isinstance(other, LVNF):
            return self.lvnf_id == other.lvnf_id

        return False

    def __str__(self):

        return "LVNF %s (nb_ports=%u)\n%s" % \
            (self.lvnf_id, self.image.nb_ports, self.image.vnf)
