#!/usr/bin/env python3

# Author: Nick Waterton <nick.waterton@med.ge.com>
# Description: OH interface to ISY controller for Inseton devices via MQTT
# N Waterton 24th September 2019 V1.0: initial release
# re-written for asyncio 9th December 2020: complete re-write
# Uses pyisy python library https://github.com/automicus/PyISY 3.x.x branch
'''
Note: fixes to be made to PyIsy library
for python 3.6 (OK for python 3.7)
isy.py and websocket.py change asyncio.get_running_loop() to
try:
    self.loop = asyncio.get_running_loop() #python 3.7 only
except AttributeError:
    self.loop = asyncio.get_event_loop()   #python 3.6
    
connection.py change if sys.version_info < (3, 7): to
if sys.version_info < (3, 6):

To fix several issues with change notifications in
node.py change async def update ...
elif hint is not None:
    # assume value was set correctly, auto update will correct errors
    self.status = hint
    _LOGGER.debug("ISY updated node: %s", self._id)
    return
    
to comment out:
    #self.status = hint     #removed _status update NW 11/12/2020 not needed (whole thing seems pointless)
'''

# import the necessary packages
import pyisy
from pyisy.constants import *
from pyisy.helpers import NodeProperty
#from pyisy.Nodes.node import parse_xml_properties
from functools import partial
import logging
from logging.handlers import RotatingFileHandler
global HAVE_MQTT
HAVE_MQTT = False
try:
    import paho.mqtt.client as paho
    from paho.mqtt.client import MQTTMessage
    HAVE_MQTT = True
except ImportError:
    print("paho mqtt client not found")
import time, os, sys, json, datetime, re
import signal
from xml.dom import minidom
import asyncio


__version__ = __VERSION__ = "2.0.0"

def pprint(obj):
    """Pretty JSON dump of an object."""
    return json.dumps(obj, sort_keys=True, indent=2, separators=(',', ': '))

def sigterm_handler(signal, frame):
    log.info('Received SIGTERM signal')
    sys.exit(0)

def setup_logger(logger_name, log_file, level=logging.DEBUG, console=False):
    try: 
        l = logging.getLogger(logger_name)
        formatter = logging.Formatter('[%(asctime)s][%(levelname)5.5s](%(name)-20s) %(message)s')
        if log_file is not None:
            fileHandler = logging.handlers.RotatingFileHandler(log_file, mode='a', maxBytes=2000000, backupCount=5)
            fileHandler.setFormatter(formatter)
        if console == True:
            #formatter = logging.Formatter('[%(levelname)1.1s %(name)-20s] %(message)s')
            streamHandler = logging.StreamHandler()
            streamHandler.setFormatter(formatter)

        l.setLevel(level)
        if log_file is not None:
            l.addHandler(fileHandler)
        if console == True:
          l.addHandler(streamHandler)
             
    except Exception as e:
        print("Error in Logging setup: %s - do you have permission to write the log file??" % e)
        sys.exit(1)
    
class isy_handler(pyisy.ISY):

    battery_device = ['BinaryAlarm_ADV', 'RemoteLinc2_ADV']
    control_event_translate =  {'DON'   : {'topic':'/onoff', 'value':'ON'},
                                'DOF'   : {'topic':'/onoff', 'value':'OFF'},
                                'DFON'  : {'topic':'/fastonoff', 'value':'ON'},
                                'DFOF'  : {'topic':'/fastonoff', 'value':'OFF'},
                                'FDUP'  : {'topic':'/dim', 'value':1},
                                'FDDOWN': {'topic':'/dim', 'value':-1},
                                'FDSTOP': {'topic':'/dim', 'value':0},
                               }

    def __init__(self, address, port, username, password,
                 use_https=False, tls_ver=None, log=None, mqttc=None, pub_topic='', sub_topic=None, use_websocket=False, loop=None):
        super().__init__(address, port, username, password, use_https, tls_ver, use_websocket=use_websocket)
        self.log = logging.getLogger('pyisy')
        self.mqttc = mqttc
        self.pub_topic = pub_topic
        self.sub_topic = sub_topic
        self.use_https = use_https
        self.use_websocket = use_websocket
        if loop:
            self.loop = loop
        else:
            self.loop = asyncio.get_event_loop()
        self._exit = False
        self.handler = {}
        self.event_handler = {}
        self.pending_commands = {}
        self.semaphore = asyncio.Semaphore(1)   #set the number of simultaneous commands that can be sent to the ISY (1 is minimum)
        self.q = asyncio.Queue(loop=self.loop)
        
    def connect(self):
        self.loop.run_until_complete(self._connect())
        
    async def _connect(self):
        self.log.info('Connecting to ISY')
        while not self.connected:
            await self.initialize()
            break
        else:
            await asyncio.sleep(10)
            self.log.warning('Retrying connection to ISY')
        self.modify_nodes()
        if self.mqttc:
            self.setup_mqtt()
        self.setup_events_subscriptions()
        if not self.use_websocket:
            self.auto_update = True  
        else:
            self.websocket.start()
        self.log.info('auto_reconnect : {}'.format(self.auto_reconnect))
        await self.process_message_queue()
        
    def disconnect(self):
        self.loop.run_until_complete(self._disconnect())
        
    async def _disconnect(self):
        '''
        disconnect and shutdown
        '''
        self._exit = True
        await self.q.put(None)
        await self.shutdown()
        while self._exit:
            await asyncio.sleep(0.1)
        self._connected = False
        self.log.info('Disconnected')
            
    def modify_nodes(self):
        '''
        add my own features
        '''
        self.log.info('Fixing issues')
        return
        #add constants
        #UOM_FRIENDLY_NAME['100']='byte'
        #COMMAND_FRIENDLY_NAME['QUERY']='Query'
                       
    def setup_mqtt(self):
        while not self.connected:
            time.sleep(5)
            self.log.info('Waiting for ISY connection')
        self.log.info('ISY Connected')
        self.mqttc.on_message = self.on_message
        self.mqttc.on_connect = self.on_connect
        if self.sub_topic:
            self.mqttc.subscribe(self.sub_topic +"#", 0)
            
    def setup_events_subscriptions(self):
        '''
        Subscribe to node changed events, and node control events
        only for nodes family type None(insteon) and Zwave
        '''
        self.log.info("got %s nodes, device: %s, scenes: %s" % (len(self.nodes.nobjs), len(self.get_nodes()), len(self.get_scenes())))
        for node in self.get_nodes():
            try:
                self.log.info("Found node: id:%s, name:%s, type:%s, dimmable:%s, unit:%s(%s), aux:%s, prec:%s, children: %s, status:%s(%s)" % (node.address, node.name, node.node_def_id, node.is_dimmable, node.uom, self.uom_name(node), node.aux_properties, node.prec, node.has_children, node.status, node.formatted))
                self.handler[node.address] = node.status_events.subscribe(partial(self.changed, node=node))
                self.event_handler[node.address] = node.control_events.subscribe(partial(self.handle_event, node=node))
                self.publish_node(node)
                
            #except Exception as e:
            #    self.log.exception(e)
            
            except (AttributeError, ValueError):
                #self.log.error("Skipping Found node: {}".format(node))
                pass
            
        self.log.info('found %d Nodes' % len(self.handler))
        
    def on_connect(self, mosq, userdata, flags, rc):
        #log.info("rc: %s" % str(rc))
        self.log.info("Reconnected to MQTT Server")
        if self.sub_topic:
            self.mqttc.subscribe(self.sub_topic +"#", 0)
        
    def get_nodes(self):
        '''
        return list of all nodes of family None(insteon) or Zwave
        '''
        return [node[1] for node in self.nodes if node[1].family in [None, 'ZWave']]
        
    def get_scenes(self):
        '''
        return list of all nodes of type 'group'
        '''
        return [node[1] for node in self.nodes if node[1].family in ['Group']]
            
    async def send_cmd(self, node, cmd, query=None, ok404=False):
        '''
        Generic Send a command to the node (query is a dictionary)
        '''
        if isinstance(cmd, list):
            req=cmd
            cmd = '/'.join(cmd)
        else:
            req = cmd.split('/')
        req_url = node.isy.conn.compile_url(req, query)
        response = await node.isy.conn.request(req_url, ok404=ok404)
        if response is None:
            self.log.warning("ISY could not send special command {} to {}.".format(cmd, node.name))
        else:
            self.log.info("ISY special command {} sent to {}".format(cmd, node.name))
        return response
        
    def check_commands_sent(self, delay=5):
        try:
            self.check_commands.cancel()
        except Exception:
            pass
        self.check_commands = self.loop.call_later(delay, self.resend_command)
        
    def update_pending_command(self, node, value=None):
        '''
        update pending commands (for resend attempt)
        '''
        if value is not None:
            if node.family == 'Group':
                self.log.debug('NODE {}({}) is a Scene - not adding to retry list'.format(node.address, node.name))
            elif node.status != value:
                self.log.debug('NODE ({}) current value: {}, set to value: {} - adding to retry list'.format(node.name, node.status, value))
                if node.uom == "100":
                    value = self.map_range(value, 0, 255, 0, 100)
                if node.name in self.pending_commands.keys():   #increment number of retries
                    value = (value, self.pending_commands[node.name][1]+1)
                else:
                    value = (value, 0)
                self.pending_commands[node.name]=value
                self.log.debug('pending commands: {}'.format(self.pending_commands))
                self.check_commands_sent()
                return
            else:
                self.log.debug('NODE ({}) current value: {}, set to value: {} - no retry required'.format(node.name, node.status, value))
        result = self.pending_commands.pop(node.name, None)
        if result is not None:
            self.log.debug('removed {} value: {} from  pending_commands'.format(node.name, result))
        
    def resend_command(self):
        '''
        resend commands in self.pending_commands
        intended to be run 5 seconds or so after command sent to PLM
        '''
        self.log.debug('Running resend_command: {}'.format('no commands pending' if len(self.pending_commands.keys())==0 else self.pending_commands))
        for name, value in self.pending_commands.copy().items():
            if value[1] <=5:    #max 5 retries
                msg = MQTTMessage(topic=name.encode())
                msg.payload = str(value[0]).encode()
            else:
                self.log.error('Too many retries on sending command {}, {} - giving up'.format(name, value[0]))
                del self.pending_commands[name]
                self.publish_node(self.nodes[name])
                return
            self.log.warning('resending command {}, {} retry: {}'.format(name, value[0], value[1]))
            self.q.put_nowait(msg)
        
    def on_message(self, mosq, obj, msg):
        self.log.info("MQTT message received topic: %s value:%s" % (msg.topic, msg.payload))
        #self.loop.call_soon_threadsafe(self.q.put_nowait, msg) #slow
        asyncio.run_coroutine_threadsafe(self.q.put(msg), self.loop)
        
    async def process_message_queue(self):
        '''
        coroutine to process messages in queue, runs until self._exit is True
        This is the main loop
        '''
        while not self._exit:
            try:
                self.log.info('process_message queue waiting for message')
                #await asyncio.sleep(0.5) # time between Insteon commands to prevent them being sent too fast (semaphore should handle this).
                msg = await self.q.get()
                if msg is not None:
                    async with self.semaphore:
                        await self.execute_message(msg)
                    #self.loop.create_task(self.execute_message(msg))   #causes errors when hitting the ISY too hard with requests
                self.q.task_done()
            except Exception as e:
                self.log.exception(e)
        self._exit = False
            
    async def execute_message(self, msg):
        '''
        Parse MQTT message and take appropriate action
        topic is the device (node name or address), payload is the action (on, off, value etc.)
        '''
        #self.log.debug('message being processed: topic: {} value:{}'.format(msg.topic, msg.payload))
        node_name = msg.topic.split('/')[-1]
        value = msg.payload.decode('utf-8').strip()
        faston = False
        self.log.info('command is : {}, {}'.format(node_name, value))
        try:
            if value.lower() == 'get_all':
                for node in self.get_nodes():
                    self.publish_node(node)
                return
            elif value.lower() == 'refresh_nodes':
                await self.nodes.update()
                return
        
            if '_fastonoff' in node_name:
                self.log.info('Fastonoff command detected')
                faston = True
                node_name = node_name.replace('_fastonoff','')
            
            node = self.nodes[node_name]
            if not node:
                self.log.warning('Command: Node: %s not found' % node_name)
                return
                
            if node.family == 'Group':
                #it's a scene, and scenes don't have UOM, so fake it
                node.uom = '100' #percent type range 0-100
                
            #self.log.debug('node: %s info: %s' % (node.name, node.__dict__))

            if 'rgb' in value.lower():    #rgb color command format 'rgb r,g,b' 0-255 per component
                await self.send_rgb_command(node, value)
            elif value.startswith('rest/'):    #directly send rest command to node
                await self.send_rest_command(node, value)
            elif '?' in value:
                val = value.split('?')
                await node.send_cmd(val[0], query=json.loads(val[1]))
            elif 'query' in value.lower():
                await self.query_node(node)
            elif 'get' in value.lower():
                self.publish_node(node)
            elif 'debug' in value.lower():
                self.log.setLevel(5)    #highest debug logging level
            elif await self.node_on_off(node, value, faston):
                self.log.info('Finished processing MQTT node: {} ({}), value: {} faston: {}, as on/off/dim command'.format(node.address, node.name, value, faston))
            else:
                self.log.warning('Unable to send value: {} to node: {}({})'.format(value, node.address, node.name))
                
        except Exception as e:
            self.log.exception(e)
           
    async def send_rgb_command(self, node, value):
        val = value.lower().replace('rgb','').split(',')
        if len(val) != 3:
            raise ValueError('RGB value sent is incorrect: %s' % val)
        self.log.info('rgb value: %s received' % val)
        rgb = {}
        for i, v in enumerate(val, 2):
            rgb['GV%s' % i] = int(v)
        await node.send_cmd('DON', query=rgb)
        
    async def send_rest_command(self, node, value):
        val = value.split('/')[1:]
        resp = await self.send_cmd(node, val)
        self.log.info('Received response: %s' % resp)
        if resp:
            self.publish_node(node, text=resp)
        
    async def node_on_off(self, node, value, faston=False):
        '''
        send insteon node command according to value
        '''
        self.log.info('Running node_on_off: {} ({}), {}, faston:{}, uom: {}'.format(node.address, node.name, value, faston, node.uom))
        value = self.convert_value(node, value)   
        self.update_pending_command(node, value)    #for resend attempt if needed

        if faston:
            if value:
                return await node.fast_on()
            else:
                return await node.fast_off()
                
        else:
            return await node.turn_on(value if value < 255 else None)
        
        return False
        
    def convert_value(self, node, value):
        '''
        convert string on/off/percent/value to 0-255 integer
        '''
        value = value.upper()
        if 'ON' in value:
            value = 255
        elif 'OFF' in value:
            value = 0
        if isinstance(value, str):
            value = int(value.split('.')[0])
            if node.uom == "100":
                value = self.map_range(value, 0, 100, 0, 255)
        return value
        
    def publish(self, topic='', message=''):
        if self.mqttc:
            if topic and not topic.startswith('/'):
                topic = '/'+topic
            self.log.info('publishing: {} {}'.format(self.pub_topic+topic, message))
            self.mqttc.publish(self.pub_topic+topic, message)
        
    def publish_node(self, node, text=None):
        '''
        Publish node settings, or control events (not change events)
        event is expected to be a NodeProperty object, so we can treat it similarly to a node
        '''
        base_node = self.nodes[node.address]
        name = base_node.name.strip()
        if text:
            name += '/text'
            value = text
        elif isinstance(node, NodeProperty):
            node.last_update = datetime.datetime.now()  #NodeProperty doesn't have last_update
            if node.control != 'ERR' and node.value != 1: 
                self.update_pending_command(base_node)  #remove from pending command list
            vals = self.control_event_translate.get(node.control)
            if vals is not None:
                name += vals['topic']
                value = vals['value']
            else:
                name+='/'+node.control
                value = self.scale_value(node)
        else:
            self.update_pending_command(base_node)      #remove from pending command list
            value = self.scale_value(node)
            
        if value is not None:
            self.log.info('publishing: {} {} (UOM:{}({}))'.format(name, value, node.uom, self.uom_name(node)))
            self.publish(name, value)
            #self.publish(name+'/formatted', node.formatted)
            #publishes 'lastupdate' datetime in iso format, NOTE OH2 doesn't support microseconds (only milliseconds) - 2.5.0 might support milliseconds now 
            #self.publish(name+'/lastupdate', datetime.datetime.now().replace(microsecond=0).isoformat())
            self.publish(name+'/lastupdate', node.last_update.isoformat())

    def scale_value(self, node):
        '''
        Returns node or NodeProperty values scaled to the correct range depending on UOM
        Basically just extracts the node.formatted property (which now reports correctly)
        returns string in upper case, or None
        '''
        try:
            if isinstance(node, NodeProperty):
                #it's a control event
                node.status = node.value    #NodeProperty doesn't have status
                #kludgey fix for RelayLampSwitch_ADV ramp rates not being formatted (probably because they don't have one)
                if node.control == 'RR' and self.nodes[node.address].node_def_id == 'RelayLampSwitch_ADV':
                    node.formatted = '{} seconds'.format(INSTEON_RAMP_RATES.get(str(node.value), node.value))
            
            value = re.search(r'-?\d+\.?\d*', str(node.formatted)) #extract number from node.formatted string
            
            if value:
                value = float(value.group())
                value = int(value) if value.is_integer() else value
                if node.uom == '100' and value >= 100: value = 'ON'
            else:
                value = node.formatted  #defined value, open, closed, off etc. usually string (but could be a number).
                
            value = str(value).upper()
                 
            self.log.debug('({}) autoscaled value(precision: {}): {} {} formatted: {} status: {}'.format(node.address, node.prec, value, self.uom_name(node), node.formatted, node.status))
            return value

        except Exception as e:
            self.log.exception(e)
            return None
            
    def uom_name(self, node, default=''):
        '''
        Get UOM name
        '''
        if default == '':
            default = '({})unknown'.format(node.uom)
        return UOM_FRIENDLY_NAME.get(str(node.uom), default)
        
    def map_range(self, var, old_min, old_max, new_min, new_max, fp=False):
        '''
        map variable from old range to new range
        '''
        old_range = float(old_max - old_min)
        new_range = float(new_max - new_min)
        new_var = (((float(var) - old_min) * new_range) / old_range) + float(new_min)
        #self.log.debug('map_range: %s maps to %.2f (int: %s)' % (var, new_var, int(round(new_var))))
        if not fp:
            return int(round(new_var))
        else:
            return round(new_var,1)  
          
    def changed(self, event, node):
        '''
        changed event is a dictionary eg
        {'address': 'ZW003_143', 'status': 0, 'last_changed': datetime.datetime(2020, 12, 4, 12, 59, 18, 377314), 'last_update': datetime.datetime(2020, 12, 4, 12, 59, 18, 377271)}
        not to be confused with a NodeProperty control event
        '''
        self.log.debug('CHANGE Notification Received {}'.format(event))
        if event['status'] == ISY_VALUE_UNKNOWN:
            self.log.debug('Ignoring CHANGE Notification value with unknown status')
        else:
            error_status = node.aux_properties.get('ERR')
            if error_status and error_status.value == 1:
                self.log.warning('Node is in error state - ignoring CHANGE Notification')
            else:
                self.log.info("CHANGE Notification Received: Node: {}({}) {}({})".format(node.address, node.name, event['status'], self.scale_value(node)))
                self.publish_node(node)
            
    def handle_event(self, event, node):
        '''
        handle control event, which is a NodeProperty, not a dictionary
        example control event:
        NodeProperty('4E 3A 5B 1': control='ERR', value='0', prec='0', uom='', formatted='0')
        NodeProperty('4E 3A 5B 1': control='ERR', value='0', prec='0', uom='', formatted='0')
        NodeProperty('27 4A 41 1': control='RR', value='28', prec='0', uom='57', formatted='0.5 seconds')
        NodeProperty('27 4A 41 1': control='OL', value='255', prec='0', uom='100', formatted='100%')
        NodeProperty('ZW002_1': control='BATLVL', value='100', prec='0', uom='51', formatted='100%')
        '''
        self.log.debug('Node {} ({}) received EVENT {}'.format(node.address, node.name, event))
        if event.value == ISY_VALUE_UNKNOWN:
            self.log.debug('Ignoring event with unknown value')
        else:
            self.log.info('Node {} ({}) received EVENT {}({}), value: {}, uom: {}({}) formatted: {}'.format(node.address,
                                                                                                            node.name,
                                                                                                            event.control,
                                                                                                            COMMAND_FRIENDLY_NAME.get(event.control, 'Unknown'),
                                                                                                            event.value,
                                                                                                            event.uom,
                                                                                                            self.uom_name(event),
                                                                                                            event.formatted))
            self.publish_node(event)
        
    async def query_node(self, node):
        '''
        query node value
        '''
        try:
            if node.node_def_id in self.battery_device:
                self.log.info('Battery powered device, so NOT Querying node: {} ({})'.format(node.address, node.name))
                return False
            self.log.info('Querying node: {} ({})'.format(node.address, node.name))
            res = await node.query()
            if res:
                return res
            else:
                self.log.warning('ISY could not query node: {} ({}) - {}'.format(node.address, node.name, res))
        except AttributeError:
            pass
        return False
        
    def get_siren_info(self, node, mode):
        '''
        parse siren info. NOTE: minidom is ridiculous.
        WIP
        '''
        try:
            if not hasattr(node, 'last_xml'):
                self.log.info('getting siren %s info: %s (%s)' % (mode, node.address, node.name))
                req_url = self.conn.compile_url(['nodes', node.address, 'get', mode])
                xml = self.conn.request(req_url)
            else:
                xml = node.last_xml
            if xml is not None:
                self.log.info('ISY siren %s, got: %s (%s) %s' % (mode, node.address, node.name, xml))
                try:
                    xmldoc = minidom.parseString(xml)
                except:
                    self.log.error('Could not parse update, ' + 'poorly formatted XML.')
                    return False
                else:
                    if len(xmldoc.getElementsByTagName('property')) > 0:
                        attrs = xmldoc.getElementsByTagName('property')[0].attributes
                        value = int(attrs['value'].value)
                        text = attrs['formatted'].value
                        uom = int(attrs['uom'].value)
                    else:
                        attrs = xmldoc.getElementsByTagName('action')[0]
                        value = int(attrs.childNodes[0].nodeValue)
                        text = xmldoc.getElementsByTagName('fmtName')[0].firstChild.nodeValue
                        uom = int(attrs.attributes['uom'].value)
                    if mode == 'DUR':
                        percent = (value*255)//uom    #255 = 100%
                        self.log.info('Siren duration: %s, %s out of %s (%s%%)' % (text, value, uom, percent//2.55))
                        node.status.update(percent, force=True, silent=True)
                    else:
                        self.log.info('Siren %s: %s, %s out of %s' % (mode, text, value, uom))
                    return True
            else:
                self.log.warning('ISY could not query node: %s (%s) - %s' % (node.address, node.name, xml))
                return False
        except AttributeError:
            pass
        except Exception as e:
            self.log.exception(e)
        return False
    
def main():
    '''
    Main routine
    '''
    global log
    import argparse
    parser = argparse.ArgumentParser(description='ISY MQTT-WS Client and Control')
    #required_flags = parser.add_mutually_exclusive_group(required=True)
    # Required flags go here.
    parser.add_argument('-ip','--ipaddress', action="store", default=None, help='ISY IP address')
    parser.add_argument('-po','--isyport', action="store", default=None, help='ISY port')
    parser.add_argument('-U','--isyuser', action="store", default=None, help='ISY user (used for login)')
    parser.add_argument('-P','--isypassword', action="store", default=None, help='ISY password (used for login)')
    parser.add_argument('-s', "--secure", action="store_true", default=False, help='use HTTPS/WSS for communications (default: %(default)s')
    parser.add_argument('-ws', "--websocket", action="store_true", default=False, help='use Websocket for communications (default: %(default)s')
    #parser.add_argument('-cid','--client_id', action="store", default=None, help='optional MQTT CLIENT ID  (default: %(default)s')
    parser.add_argument('-b','--broker', action="store", default=None, help='mqtt broker to publish sensor data to.  (default: %(default)s')
    parser.add_argument('-p','--port', action="store", type=int, default=1883, help='mqtt broker port (default: %(default)s')
    parser.add_argument('-u','--user', action="store", default=None, help='mqtt broker username. (default: %(default)s')
    parser.add_argument('-pw','--password', action="store", default=None, help='mqtt broker password. (default: %(default)s')
    parser.add_argument('-pt','--pub_topic', action="store",default='/isy_status', help='topic to publish ewelink data to. (default: %(default)s')
    parser.add_argument('-st','--sub_topic', action="store",default='/isy_command/', help='topic to publish ewelink commands to. (default: %(default)s')
    parser.add_argument('-poll','--poll', action="store", type=int, default=15, help='polling period (default: %(default)s')
    parser.add_argument('-l','--log', action="store",default="None", help='log file. (default: %(default)s')
    parser.add_argument('-D','--debug', action='store_true', help='debug mode', default = False)
    parser.add_argument('-V','--version', action='version',version='%(prog)s {version}'.format(version=__VERSION__))
    
    
    arg = parser.parse_args()
    
    if arg.debug:
      log_level = 5 #logging.DEBUG
    else:
      log_level = logging.INFO
    
    #setup logging
    if arg.log == 'None':
        log_file = None
    else:
        log_file=os.path.expanduser(arg.log)
        
    setup_logger('pyisy',log_file,level=log_level,console=not arg.websocket)
    setup_logger('Main',log_file,level=log_level,console=True)
    
    log = logging.getLogger('Main')
    
    log.debug('Debug mode')
    
    log.info("Python Version: %s" % sys.version.replace('\n',''))
    log.info('PyISY Version: V:%s' % pyisy.__version__)
    
    #register signal handler
    signal.signal(signal.SIGTERM, sigterm_handler)
    
    #ISY connection
    isy_ip = arg.ipaddress
    if arg.isyport is None:
        if arg.secure:
            isy_port = 443
        else:
            isy_port = 80
    else:
        isy_port = arg.isyport
    isy_user = arg.isyuser
    isy_pass = arg.isypassword

    broker = arg.broker
    port = arg.port
    user = arg.user
    password = arg.password
    
    if not HAVE_MQTT:
        broker = None
        log.critical('You must have the paho-mqtt library installed')
        sys.exit(2)
    
    mqttc = None
    loop = None
    isy = None
    
    if not broker:
        log.critical('You must define an MQTT broker')
        parser.print_help()
        sys.exit(2)
        
    if arg.poll > 0:
        log.warning('poll argument is depreciated - ignoring')
        
    if arg.websocket:
        log.info('Using websocket connection')
    else:
        log.info('Using tcp socket connection')
        
    log.info('secure connection: {}'.format(arg.secure))

    try:

        mqttc = paho.Client()               #Setup MQTT
        mqttc.will_set(arg.pub_topic+"/client/status", "Offline at: %s" % time.ctime(), 0, False)
        if user is not None and password is not None:
            mqttc.username_pw_set(username=user,password=password)
        mqttc.connect(broker, port, 120)
        mqttc.loop_start()

        loop = asyncio.get_event_loop()
        if arg.debug:
            # Enable debugging
            #loop.set_debug(True)
            pass
        
        isy = isy_handler(isy_ip, isy_port, isy_user, isy_pass, arg.secure, 1.2, log, mqttc, arg.pub_topic, arg.sub_topic, arg.websocket, loop)
        isy.connect()   #blocking connect
        
        
    except (KeyboardInterrupt, SystemExit):
        log.info("System exit Received - Exiting program")
            
    except Exception as e:
        if log_level == logging.DEBUG:
            log.exception("Error: %s" % e)
        else:
            log.error("Error: %s" % e)
        
    finally:
        if isy and isy.connected:
            isy.disconnect()
            log.info('isy disconnected') 
        if loop:
            loop.close()
        if mqttc:
            mqttc.loop_stop()
            mqttc.disconnect()
        
        log.debug("Program Exited")
      

if __name__ == "__main__":
    main()

