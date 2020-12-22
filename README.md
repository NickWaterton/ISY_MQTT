# ISY_MQTT
MQTT to Universal Devices ISY944i bridge. Allows control of Insteon and Zwave devices

Uses PyIsy library V3.x.x

```
nick@MQTT-Servers-Host:~/Scripts/ISY_MQTT$ ./ISY_bridge.py -h
usage: ISY_bridge.py [-h] [-ip IPADDRESS] [-po ISYPORT] [-U ISYUSER]
                     [-P ISYPASSWORD] [-s] [-ws] [-b BROKER] [-p PORT]
                     [-u USER] [-pw PASSWORD] [-pt PUB_TOPIC] [-st SUB_TOPIC]
                     [-poll POLL] [-l LOG] [-D] [-V]

ISY MQTT-WS Client and Control

optional arguments:
  -h, --help            show this help message and exit
  -ip IPADDRESS, --ipaddress IPADDRESS
                        ISY IP address
  -po ISYPORT, --isyport ISYPORT
                        ISY port
  -U ISYUSER, --isyuser ISYUSER
                        ISY user (used for login)
  -P ISYPASSWORD, --isypassword ISYPASSWORD
                        ISY password (used for login)
  -s, --secure          use HTTPS/WSS for communications (default: False
  -ws, --websocket      use Websocket for communications (default: False
  -b BROKER, --broker BROKER
                        mqtt broker to publish sensor data to. (default: None
  -p PORT, --port PORT  mqtt broker port (default: 1883
  -u USER, --user USER  mqtt broker username. (default: None
  -pw PASSWORD, --password PASSWORD
                        mqtt broker password. (default: None
  -pt PUB_TOPIC, --pub_topic PUB_TOPIC
                        topic to publish ewelink data to. (default:
                        /isy_status
  -st SUB_TOPIC, --sub_topic SUB_TOPIC
                        topic to publish ewelink commands to. (default:
                        /isy_command/
  -poll POLL, --poll POLL
                        polling period (default: 15
  -l LOG, --log LOG     log file. (default: None
  -D, --debug           debug mode
  -V, --version         show program's version number and exit
```
