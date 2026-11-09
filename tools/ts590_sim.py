#!/usr/bin/env python3
"""Minimal TS-590SG simulator on a pty (for testing kx2probe without hardware).
Start: python3 ts590_sim.py  -> prints the pty name; then: python3 kx2probe.py --rig ts590 --port <name>"""
import os, pty, random, select, time
st = {"FA":"00014025000","FB":"00014030000","FR":"0","FT":"0","MD":"3","DA":"0","FW":"0500","SH":"07","SL":"03",
      "IS":" 0600","RT":"0","XT":"0","IF":"00014025000     +000000000300000000 ","RA":"0000","PA":"1","NB":"0","NL":"005",
      "NR":"0","RL":"05","NT":"00","BP":"064","BC":"0","GC":"2","GT":"05","AG":"0100","RG":"255","SQ":"0000","KS":"026",
      "SD":"0000","VX":"1","PC":"050","TP":"010","AC":"110","CA":"0","AN":"199","LK":"0","BY":"00","SM":"00005","KY":"0",
      "FV":"1.04","ID":"023"}
m, s = pty.openpty(); print("TS-590SG sim:", os.ttyname(s), flush=True)
buf=b""; ai=0; tn=time.time()+1
while True:
    r,_,_=select.select([m],[],[],0.05)
    if r:
        buf+=os.read(m,1024)
        while b";" in buf:
            f,buf=buf.split(b";",1); f=f.decode(); c,a=f[:2],f[2:]
            time.sleep(0.008)
            if c=="AI":
                if a: ai=int(a)
                else: os.write(m,b"AI%d;"%ai)
            elif c=="RM": os.write(m,("RM1%04d;RM2%04d;RM3%04d;"%(random.randint(0,4),0,random.randint(0,10))).encode())
            elif c in ("SM","AG","SQ") and a in ("0",""): os.write(m,(c+st[c]+";").encode()) if c!="SM" else os.write(m,("SM0"+st["SM"][1:]+";").encode())
            elif c in st and not a: os.write(m,(c+st[c]+";").encode())
            elif c in st: st[c]=a
            else: os.write(m,b"?;")
    if time.time()>tn:
        tn=time.time()+1; st["FA"]="%011d"%(int(st["FA"])+10); st["NR"]=random.choice("012")
        if ai: os.write(m,("FA"+st["FA"]+";NR"+st["NR"]+";").encode())
