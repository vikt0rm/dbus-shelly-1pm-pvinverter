#!/usr/bin/env python
 
# import normal packages
import platform 
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file

from logging.handlers import TimedRotatingFileHandler
 
# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusShelly1pmService:
  def __init__(self, logger, servicename, paths, productname='Shelly 1PM', connection='Shelly 1PM HTTP JSON service'):
    config = self._getConfig()
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    customname = config['DEFAULT']['CustomName']
    
    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
    self._paths = paths
    self._logger = logger
    
    self._logger.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
    
    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)
    
    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    #self._dbusservice.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
    self._dbusservice.add_path('/ProductId', 0xFFFF) # id assigned by Victron Support from SDM630v2.py
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', customname)    
    self._dbusservice.add_path('/Connected', 1)
    
    self._dbusservice.add_path('/Latency', None)    
    self._dbusservice.add_path('/FirmwareVersion', 0.1)
    self._dbusservice.add_path('/HardwareVersion', 0)
    self._dbusservice.add_path('/Position', 0) # normaly only needed for pvinverter
    self._dbusservice.add_path('/Serial', self._getShellySerial())
    self._dbusservice.add_path('/UpdateIndex', 0)
    self._dbusservice.add_path('/StatusCode', 0)  # Dummy path so VRM detects us as a PV-inverter.
    
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # last update
    self._lastUpdate = 0

    # add _update function 'timer'
    gobject.timeout_add(250, self._update) # pause 250ms before the next request
    
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)
 
  def _getShellySerial(self):
    meter_data = self._getShellyData()  
    
    if not meter_data['mac']:
        raise ValueError("Response does not contain 'mac' attribute")
    
    serial = meter_data['mac']
    return serial
 
 
  def _getConfig(self):
    config = configparser.ConfigParser()
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config;
 
 
  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']
    
    if not value: 
        value = 0
    
    return int(value)
  
  
  def _getShellyStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    
    if accessType == 'OnPremise': 
        URL = "http://%s:%s@%s/status" % (config['ONPREMISE']['Username'], config['ONPREMISE']['Password'], config['ONPREMISE']['Host'])
        URL = URL.replace(":@", "")
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
    
 
  def _getShellyData(self):
    URL = self._getShellyStatusUrl()
    meter_r = requests.get(url = URL)
    
    # check for response
    if not meter_r:
        raise ConnectionError("No response from Shelly 1PM - %s" % (URL))
    
    meter_data = meter_r.json()     
    
    # check for Json
    if not meter_data:
        raise ValueError("Converting response to JSON failed")
    
    
    return meter_data
 
 
  def _signOfLife(self):
    self._logger.info("--- Start: sign of life ---")
    self._logger.info("Last _update() call: %s" % (self._lastUpdate))
    self._logger.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    self._logger.info("--- End: sign of life ---")
    return True
 
  def _update(self):   
    try:
       #get data from Shelly 1pm
       meter_data = self._getShellyData()
       
       config = self._getConfig()
       str(config['DEFAULT']['Phase'])
    
       pvinverter_phase = str(config['DEFAULT']['Phase'])
       
       #send data to DBus
       for phase in ['L1', 'L2', 'L3']:
         pre = '/Ac/' + phase
         
         if phase == pvinverter_phase:
           power = meter_data['meters'][0]['power']
           total = meter_data['meters'][0]['total']
           voltage = 230
           current = power / voltage
           
           self._dbusservice[pre + '/Voltage'] = voltage
           self._dbusservice[pre + '/Current'] = current
           self._dbusservice[pre + '/Power'] = power
           if power > 0:
             self._dbusservice[pre + '/Energy/Forward'] = total/1000/60 
           
         else:
           self._dbusservice[pre + '/Voltage'] = 0
           self._dbusservice[pre + '/Current'] = 0
           self._dbusservice[pre + '/Power'] = 0
           self._dbusservice[pre + '/Energy/Forward'] = 0
           
       self._dbusservice['/Ac/Power'] = self._dbusservice['/Ac/' + pvinverter_phase + '/Power']
       self._dbusservice['/Ac/Energy/Forward'] = self._dbusservice['/Ac/' + pvinverter_phase + '/Energy/Forward']
       
       #logging
       self._logger.debug("House Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
       self._logger.debug("House Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
       self._logger.debug("---");
       
       # increment UpdateIndex - to show that new data is available
       index = self._dbusservice['/UpdateIndex'] + 1  # increment index
       if index > 255:   # maximum value of the index
         index = 0       # overflow from 255 to 0
       self._dbusservice['/UpdateIndex'] = index

       #update lastupdate vars
       self._lastUpdate = time.time()              
    except Exception as e:
       self._logger.critical('Error at %s', '_update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True
 
  def _handlechangedvalue(self, path, value):
    self._logger.debug("someone else updated %s to %s" % (path, value))
    return True # accept the change
 

def main():
  #configure logging
#  logger.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
#                            datefmt='%Y-%m-%d %H:%M:%S',
#                            level=logging.INFO,
#                            handlers=[
#                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
#                                logging.StreamHandler()
#                            ])

  logger = logging.getLogger("Rotating Log")
  logger.setLevel(logging.INFO)

  handler = TimedRotatingFileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__))),
                                     when="d",
                                     interval=1,
                                     backupCount=7)
  logger.addHandler(handler)
 
  try:
      logger.info("Start");
  
      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      #formatting 
      _kwh = lambda p, v: (str(round(v, 2)) + 'KWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')   
     
      #start our main-service
      pvac_output = DbusShelly1pmService(logger,
        servicename='com.victronenergy.pvinverter',
        paths={
          '/Ac/Energy/Forward': {'initial': None, 'textformat': _kwh}, # energy produced by pv inverter
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          
          '/Ac/Current': {'initial': 0, 'textformat': _a},
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          
          '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L2/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L3/Voltage': {'initial': 0, 'textformat': _v},
          '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L2/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L3/Current': {'initial': 0, 'textformat': _a},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L2/Energy/Forward': {'initial': None, 'textformat': _kwh},
          '/Ac/L3/Energy/Forward': {'initial': None, 'textformat': _kwh},
        })
     
      logger.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()            
  except Exception as e:
    logger.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
