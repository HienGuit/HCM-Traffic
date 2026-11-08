"""Small synchronized console/file presentation boundary, without credentials."""
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import re
import threading
import traceback


class Progress:
    def __init__(self,path,clock=None,timezone='Asia/Ho_Chi_Minh'):
        self.path=path;self.clock=clock;self.timezone=timezone;self.lock=threading.Lock()
        path.parent.mkdir(parents=True,exist_ok=True)

    def clean(self,value):
        text=str(value)
        secret=os.environ.get('TOMTOM_API_KEY')
        if secret:text=text.replace(secret,'[REDACTED]')
        return re.sub(r'(?i)(key|authorization|token)=([^\s&]+)',r'\1=[REDACTED]',text)

    def __call__(self,event,**values):
        now=self.clock.now_utc() if self.clock else datetime.now(ZoneInfo(self.timezone))
        stamp=now.astimezone(ZoneInfo(self.timezone)).isoformat(timespec='seconds')[11:]
        level=values.pop('level','INFO')
        line=self.clean(f'[{stamp}] {level} {event} '+ ' '.join(f'{k}={v}' for k,v in values.items()))
        with self.lock:
            try:print(line,flush=True)
            except OSError:pass
            try:
                with self.path.open('a',encoding='utf-8') as stream:stream.write(line+'\n')
            except OSError:pass  # Presentation must not change source/capture behavior.

    def failure(self,event,exc):
        self(event,level='ERROR',reason=type(exc).__name__)
        with self.lock:
            try:
                with self.path.open('a',encoding='utf-8') as stream:
                    stream.write(self.clean(''.join(traceback.format_exception(exc)))+'\n')
            except OSError:pass
