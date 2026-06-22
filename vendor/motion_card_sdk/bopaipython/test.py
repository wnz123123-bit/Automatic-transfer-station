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
for i in range(0,16):
#     
#    a+=dll. GA_SetExtDoBit(0,i,0)
    a+=dll. GA_SetExtDoBit(0,i,0)
        
#a+=dll. GA_SetExtDoBit(0,0,1)
a+=dll. GA_SetExtDoBit(0,1,1)
a+=dll. GA_SetExtDoBit(0,7,1)
print(a)
buffer = struct.pack("L", 0) 
buffer2 = struct.pack("L", 0) 

a=dll.GA_GetExtDoValue(0,buffer,1)#//filter  1  
#a=dll.GA_GetExtDoValue(0,buffer2,0)#//filter 0 don't use at the same time with 1
print(a)
print(buffer)
print(buffer[0:2])
#https://docs.python.org/3/library/functions.html#bin
bin0=bin(buffer[0])
bin1=bin(buffer[1])
print(bin0 ,'---')
print(bin1 ,'-***')
print(bin0[3])



jp=JogPrm()
jp.dSmooth = 0;
jp.dAcc = 0.5 
jp.dDec = 0.5 
Axis=1
for i in range(0,3):
    runmoto(dll,1,jp,200) 
    runmoto(dll,2,jp,200) 
    runmoto(dll,3,jp,200) 
   
    time.sleep(2)
    runstop(dll,1,jp)
    runstop(dll,2,jp)
    runstop(dll,3,jp)
    time.sleep(2)
     
    runmoto(dll,1,jp,-200) 
    runmoto(dll,2,jp,-200) 
    runmoto(dll,3,jp,-200) 
    time.sleep(2)
    runstop(dll,1,jp)
    runstop(dll,2,jp)
    runstop(dll,3,jp)
    time.sleep(2)
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