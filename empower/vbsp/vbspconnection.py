#!/usr/bin/env python3
#
# Copyright (c) 2016 Supreeth Herle
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

"""VBSP Connection."""

import time
import tornado.ioloop
import socket
import sys

from protobuf_to_dict import protobuf_to_dict

from empower.vbsp import EMAGE_VERSION
from empower.vbsp import PRT_VBSP_BYE
from empower.vbsp import PRT_VBSP_REGISTER
from empower.vbsp import PRT_VBSP_CONFIGS
from empower.vbsp import PRT_VBSP_STATS
from empower.vbsp.messages import main_pb2
from empower.vbsp.messages import configs_pb2
from empower.core.utils import hex_to_ether
from empower.core.utils import ether_to_hex
from empower.core.ue import UE

from empower.main import RUNTIME

import empower.logger
LOG = empower.logger.get_logger()


def create_header(t_id, b_id, msg_type, header):
    """Create message header."""

    if not header:
        LOG.error("header parameter is None")

    header.vers = EMAGE_VERSION
    # Set the transaction identifier (module id).
    header.t_id = t_id
    # Set the Base station identifier.
    header.b_id = b_id
    # Set the type of message.
    header.type = msg_type
    # Module identifier is always set to zero. (OAI reasons)
    header.m_id = 0
    # Start the sequence number for messages from zero.
    header.seq = 0


def serialize_message(message):
    """Serialize message."""

    if not message:
        LOG.error("message parameter is None")
        return None

    return message.SerializeToString()


def deserialize_message(serialized_data):
    """De-Serialize message."""

    if not serialized_data:
        LOG.error("Received serialized data is None")
        return None

    msg = main_pb2.emage_msg()
    msg.ParseFromString(serialized_data)

    return msg


class VBSPConnection(object):
    """VBSP Connection.

    Represents a connection to a ENB (EUTRAN Base Station) using
    the VBSP Protocol. One VBSPConnection object is created for every
    ENB in the network. The object implements the logic for handling
    incoming messages. The currently supported messages are:

    Attributes:
        stream: The stream object used to talk with the ENB.
        address: The connection source address, i.e. the ENB IP address.
        server: Pointer to the server object.
        vbs: Pointer to a VBS object.
    """

    def __init__(self, stream, addr, server):
        self.stream = stream
        self.stream.set_nodelay(True)
        self.addr = addr
        self.server = server
        self.vbs = None
        self.stream.set_close_callback(self._on_disconnect)
        self.__buffer = b''
        self._hb_interval_ms = 500
        self._hb_worker = tornado.ioloop.PeriodicCallback(self._heartbeat_cb,
                                                          self._hb_interval_ms)
        self.endian = sys.byteorder
        self._hb_worker.start()
        self._wait()

    def to_dict(self):
        """Return dict representation of object."""

        return self.addr

    def _heartbeat_cb(self):
        """Check if connection is still active."""

        if self.vbs and not self.stream.closed():
            timeout = (self.vbs.period / 1000) * 3
            if (self.vbs.last_seen_ts + timeout) < time.time():
                LOG.info('Client inactive %s at %r', self.vbs.addr, self.addr)
                self.stream.close()

    def stream_send(self, message):
        """Send message."""

        size = message.ByteSize()

        LOG.info("Sent message of length %d", size)

        size_bytes = (socket.htonl(size)).to_bytes(4, byteorder=self.endian)
        send_buff = serialize_message(message)
        buff = size_bytes + send_buff

        if buff is None:
            LOG.error("errno %u occured")

        self.stream.write(buff)

    def _on_read(self, line):
        """ Appends bytes read from socket to a buffer. Once the full packet
        has been read the parser is invoked and the buffers is cleared. The
        parsed packet is then passed to the suitable method or dropped if the
        packet type in unknown. """

        self.__buffer = b''

        if line is not None:

            self.__buffer = self.__buffer + line

            if len(line) == 4:
                temp_size = int.from_bytes(line, byteorder=self.endian)
                size = socket.ntohl(int(temp_size))
                self.stream.read_bytes(size, self._on_read)
                return

            deserialized_msg = deserialize_message(line)

            self._trigger_message(deserialized_msg)
            self._wait()

    def _trigger_message(self, deserialized_msg):

        msg_type = deserialized_msg.WhichOneof("message")

        if not msg_type or msg_type not in self.server.pt_types:
            LOG.error("Unknown message type %s", msg_type)
            return

        if msg_type == PRT_VBSP_CONFIGS:
            config_type = deserialized_msg.mConfs.WhichOneof("config_msg")
            if not config_type or config_type not in self.server.pt_types:
                LOG.error("Unknown configs message type %u", config_type)
                return
            msg_type = config_type

        if msg_type == PRT_VBSP_STATS:
            st_type = deserialized_msg.mStats.WhichOneof("stats_msg")
            if not st_type or st_type not in self.server.pt_types:
                LOG.error("Unknown stats message type %u", st_type)
                return
            msg_type = st_type

        handler_name = "_handle_%s" % self.server.pt_types[msg_type]

        if hasattr(self, handler_name):
            handler = getattr(self, handler_name)
            handler(deserialized_msg)

        if msg_type in self.server.pt_types_handlers:
            for handler in self.server.pt_types_handlers[msg_type]:
                handler(deserialized_msg)

    def _handle_hello(self, main_msg):
        """Handle an incoming HELLO message.

        Args:
            main_msg, a emage_msg containing HELLO message
        Returns:
            None
        """

        enb_id = main_msg.head.b_id
        vbs_id = hex_to_ether(enb_id)

        try:
            vbs = RUNTIME.vbses[vbs_id]
        except KeyError:
            LOG.error("Hello from unknown VBS (%s)", (vbs_id))
            return

        LOG.info("Hello from %s, VBS %s", self.addr[0], vbs_id.to_str())

        # If this is a new connection, then send enb config request (TODO).
        if not vbs.connection:
            self.vbs = vbs
            vbs.connection = self
            self.send_enb_conf_req()
            self.send_ue_conf_req()
            vbs.period = 5000
            self.send_register_message_to_self()

        # Update VBSP params
        vbs.last_seen = main_msg.head.seq
        vbs.last_seen_ts = time.time()

    def _handle_enb_conf_repl(self, message):
        """Handle an incoming eNB configuration reply.

        Args:
            message, a emage_msg containing eNB configuration message
        Returns:
            None
        """

        vbs = RUNTIME.vbses[self.vbs.addr]
        msg = protobuf_to_dict(message)

        if "cell_conf" not in msg["mConfs"]["enb_conf_repl"]:
            return

        cc_configs = msg["mConfs"]["enb_conf_repl"]["cell_conf"]

        for c_carrier in cc_configs:
            cc_id = c_carrier["cc_id"]
            vbs.cc_configs[cc_id] = c_carrier

    def _handle_ue_conf_repl(self, message):
        """Handle an incoming UE configuration reply.

        Args:
            message, a emage_msg containing UE configuration message
        Returns:
            None
        """

        vbs = RUNTIME.vbses[self.vbs.addr]
        msg = protobuf_to_dict(message)

        if "ue_conf" not in msg["mConfs"]["ue_conf_repl"]:
            return

        ue_configs = msg["mConfs"]["ue_conf_repl"]["ue_conf"]

        for ue_config in ue_configs:

            rnti = ue_config["rnti"]

            if rnti not in vbs.ues:
                vbs.ues[rnti] = UE(rnti, self.vbs, ue_config)

            if "phy_conf" in ue_config:
                vbs.ues[rnti].phy_config = ue_config["phy_conf"]

            if "mac_conf" in ue_config:
                vbs.ues[rnti].mac_config = ue_config["mac_conf"]

            if "rrc_conf" in ue_config:
                vbs.ues[rnti].rrc_config = ue_config["rrc_conf"]

    def send_enb_conf_req(self):
        """ Send request for eNB configuration """

        enb_req = main_pb2.emage_msg()

        enb_id = ether_to_hex(self.vbs.addr)

        # Transaction identifier is zero by default.
        create_header(0, enb_id, main_pb2.CONF_REQ, enb_req.head)
        conf_req = enb_req.mConfs

        conf_req.type = configs_pb2.ENB_CONF_REQUEST
        conf_req.enb_conf_req.layer = configs_pb2.LC_ALL

        LOG.info("Sending eNB config request to VBSP %s (%u)",
                 self.vbs.addr, enb_id)

        self.stream_send(enb_req)

    def send_ue_conf_req(self):
        """ Send request for UE configurations in eNB """

        ue_req = main_pb2.emage_msg()

        enb_id = ether_to_hex(self.vbs.addr)

        # Transaction identifier is zero by default.
        create_header(0, enb_id, main_pb2.CONF_REQ, ue_req.head)
        conf_req = ue_req.mConfs

        conf_req.type = configs_pb2.UE_CONF_REQUEST
        conf_req.ue_conf_req.layer = configs_pb2.LC_ALL

        LOG.info("Sending UE config request to VBSP %s (%u)",
                 self.vbs.addr, enb_id)

        self.stream_send(ue_req)

    def _wait(self):
        """ Wait for incoming packets on signalling channel """
        self.stream.read_bytes(4, self._on_read)

    def _on_disconnect(self):
        """Handle VBSP disconnection."""

        if not self.vbs:
            return

        LOG.info("VBS disconnected: %s", self.vbs.addr)

        # reset state
        self.vbs.last_seen = 0
        self.vbs.connection = None

    def send_bye_message_to_self(self):
        """Send a unsollicited BYE message to self."""

        for handler in self.server.pt_types_handlers[PRT_VBSP_BYE]:
            handler(self.vbs)

    def send_register_message_to_self(self):
        """Send a REGISTER message to self."""

        for handler in self.server.pt_types_handlers[PRT_VBSP_REGISTER]:
            handler(self.vbs)
