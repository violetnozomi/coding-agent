import sys

from PyQt5.QtCore import QCoreApplication

from qutebrowser.misc import guiprocess
from qutebrowser.utils import message

app = QCoreApplication(sys.argv)
events = []

proc = guiprocess.GUIProcess('test', output_messages=True)
proc._proc.readyRead.connect(lambda: events.append('readyRead'))
proc._proc.readyReadStandardOutput.connect(lambda: events.append('rr_out'))
proc._proc.readyReadStandardError.connect(lambda: events.append('rr_err'))


def info(text, **kwargs):
    events.append(('info', text, kwargs.get('replace')))


def error(text, **kwargs):
    events.append(('error', text, kwargs.get('replace')))


message.info = info
message.error = error


def fin(code, status):
    print("EVENTS:", events, flush=True)
    app.quit()


proc.finished.connect(fin)

script = ('import sys, time\n'
          'print("out text", flush=True)\n'
          'sys.stderr.write("err text\\n")\n'
          'sys.stderr.flush()\n'
          'time.sleep(0.5)\n')
proc.start(sys.executable, ['-c', script])
proc._proc.closeWriteChannel()

app.exec_()
