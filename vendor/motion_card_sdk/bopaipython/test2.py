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
def runmoto(dll,Axis,jp,values):
    a=0
    print('aaaa')
    a+=dll. GA_SetStopDec(Axis,1,1)
    print(a)
    a+=dll.GA_LmtsOn(Axis,0)
    
    a+=dll.GA_LmtsOn(Axis,1)
    print('bbb')
    a+= dll.GA_PrfJog(Axis)
    a+= dll.GA_SetJogPrm(Axis,jp) 
    a+=dll.GA_AxisOn(Axis)
    a+= dll.GA_SetVel(Axis,c_double( values) )
    
        
        
    a+=dll. GA_Update(0XFF)
    return a

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
    
dll = CDLL('GAS.dll')
print(dll)
a=dll.GA_Open(0,"192.168.0.200")
#a+=dll. GA_SetCardNo(1)
a+=dll. GA_Reset()
 



jp=JogPrm()
jp.dSmooth = 0
jp.dAcc = 0.5 
jp.dDec = 0.5 
Axis=1
for i in range(0,10):
    runmoto(dll,1,jp,  100) 
    
   
    time.sleep(5)
    runmoto(dll,1,jp,  -100) 
    time.sleep(5)
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