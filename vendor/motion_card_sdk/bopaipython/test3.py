from ctypes import *
import ctypes
import numpy as np
import struct 
import time
import math  

#from gmssl import func 
# typedef struct JogPrm
# {
#     double dAcc;
#     double dDec;
#     double dSmooth;
# }TJogPrm;
#https://www.cnblogs.com/pyse/p/8590829.html
#http://blog.sina.com.cn/s/blog_4c0cb1c00102xnw7.html
class JogPrm(Structure):
    _fields_ = [("dAcc",c_double),
            ("dDec", c_double),
            
            ("dSmooth", c_double)
            ]    
class TTrapPrm(Structure):
    _fields_ = [("acc",c_double),
            ("dec", c_double),
            
            ("velStart", c_int   ) ,
           
            
            ("smoothTime", c_int   )
            ]   
def runmoto(dll,Axis,jp,values):
    a=0
    print('aaaa')
    a+=dll. GA_SetStopDec(Axis,1,1)
    a+=dll.GA_LmtsOn(Axis,0)
    
    a+=dll.GA_LmtsOn(Axis,1)
    print('bbb')
    a+= dll.GA_PrfJog(Axis)
    a+= dll.GA_SetJogPrm(Axis,jp) 
    a+=dll.GA_AxisOn(Axis)
    a+= dll.GA_SetVel(Axis,c_double( values) )
    

def runstop(dll,Axis,jp):
    a=0
    a+= dll.GA_PrfJog(Axis)
    a+= dll.GA_SetJogPrm(Axis,jp) 
    a+=dll.GA_AxisOn(Axis)
    a+= dll.GA_SetVel(Axis,c_double( 0.0) )
    a+=dll. GA_Update(0XFF)
    a+=dll.GA_Stop( Axis  ,0)
    a+=dll. GA_AxisOff(Axis)
    return a
def runmototp(dll,Axis,tp,spead,values):
    a=0
    print('aaaa--')
    #a+=dll. GA_SetStopDec(Axis,1,1)
    print(a)
    #a+=dll.GA_LmtsOn(Axis,0)
    a+=dll.GA_LmtsOff(Axis,-1)
    #GA_GetSts
    
    a+=dll. GA_ClrSts(Axis, 1)
    buffera = struct.pack("l", 0) 
    bufferb = struct.pack("L", 0) 
    a+=dll. GA_GetSts(Axis,buffera, 1,bufferb)
    print('cc',a)
    #print('cc',struct.unpack('>IH',buffera))
    print('cc', buffera)
    #a+=dll.GA_LmtsOn(Axis,1)
    print('bbb---')
    a+=dll.GA_AxisOn(Axis)
    print(Axis)
    print(a)
    a+= dll.GA_PrfTrap(Axis)
    print(a)
    #a+= dll.GA_SetTrapPrm(Axis,tp) 
    a+= dll.GA_SetTrapPrmSingle(Axis,c_double(0.1),c_double(0.1),c_double(0),0)
    print('c:',a) 
    a+= dll.GA_ZeroPos(Axis,1)
    print(a)
    a+= dll. GA_SetVel(Axis,c_double( spead) )   
    print('a:',a,spead)   
    a+= dll. GA_SetPos(Axis,c_long ( values) )    
    print('b:',a, values)
    a+=dll. GA_Update(0XFF)
    print('cccc---')
    print(a)
    
    return a
    
dll = CDLL('GAS.dll')
print(dll)
a=dll.GA_Open(0,"192.168.0.200")
print(a)
#a+=dll. GA_SetCardNo(1)
a+=dll. GA_Reset()
 



tp=TTrapPrm()

tp.acc = 0.5 
tp.dec = 0.5 
tp.velStart=0
tp.smoothTime = 0
Axis=1
buffera = struct.pack("l", 0) 
bufferb = struct.pack("L", 0) 
#a+=dll.GA_GetSts(Axis,buffera,,unsigned long *pClock=NULL)
runmototp(dll,1,tp, 500, 200000) 
time.sleep(1)
# for i in range(0,10):
#     runmoto(dll,1,jp,  100) 
#     
#    
#     time.sleep(5)
#     runmoto(dll,1,jp,  -100) 
#     time.sleep(5)
    #runstop(dll,1,jp)
    #time.sleep(2)
# a+= dll.GA_PrfJog(Axis)
# a+= dll.GA_SetJogPrm(Axis,jp) 
# a+=dll.GA_AxisOn(Axis)
# a+= dll.GA_SetVel(Axis,c_double( 100.0) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( -200) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( 100) )
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( 30.0) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# #a+=dll.GA_Stop((0X0001 << (5-1)),0)
# a+= dll.GA_SetVel(Axis,c_double( 0.0) )
# a+=dll. GA_Update(0XFF)
# a+=dll.GA_Stop( 0X0001  ,0)
# a+=dll. GA_AxisOff(Axis)
# time.sleep(0.1)
# #///////////////////
# Axis=2
# a+= dll.GA_PrfJog(Axis)
# a+= dll.GA_SetJogPrm(Axis,jp) 
# a+=dll.GA_AxisOn(Axis)
# a+= dll.GA_SetVel(Axis,c_double( 100.0) )
# a+=dll. GA_Update(0XFF)
# time.sleep(2)
# a+= dll.GA_SetVel(Axis,c_double( 200) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( 300) )
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( 30.0) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# #a+=dll.GA_Stop((0X0001 << (5-1)),0)
# a+= dll.GA_SetVel(Axis,c_double( 0.0) )
# a+=dll. GA_Update(0XFF)
# a+=dll.GA_Stop( 0X0002  ,0)
# a+=dll. GA_AxisOff(Axis)
# 
# #///////////////////
# Axis=3
# a+= dll.GA_PrfJog(Axis)
# a+= dll.GA_SetJogPrm(Axis,jp) 
# a+=dll.GA_AxisOn(Axis)
# a+= dll.GA_SetVel(Axis,c_double( 100.0) )
# a+=dll. GA_Update(0XFF)
# time.sleep(2)
# a+= dll.GA_SetVel(Axis,c_double( 200) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( 300) )
# time.sleep(1)
# a+= dll.GA_SetVel(Axis,c_double( 30.0) )
# a+=dll. GA_Update(0XFF)
# time.sleep(1)
# #a+=dll.GA_Stop((0X0001 << (5-1)),0)
# a+= dll.GA_SetVel(Axis,c_double( 0.0) )
# a+=dll. GA_Update(0XFF)
# a+=dll.GA_Stop( 0X0003  ,0)
# a+=dll. GA_AxisOff(Axis)
time.sleep(3)
a=dll.GA_Close()
#list=[buffer>>d & 1 for d in range(10)][::-1]
#print(list)

#print(buffer2)