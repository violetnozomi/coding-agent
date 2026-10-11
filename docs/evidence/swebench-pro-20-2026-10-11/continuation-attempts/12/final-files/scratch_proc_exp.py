import sys
from PyQt5.QtCore import QCoreApplication, QProcess

app = QCoreApplication(sys.argv)


def run(script, label):
    proc = QProcess()
    counts = {'readyRead': 0, 'out': 0, 'err': 0}
    chunks = {'out': [], 'err': []}
    proc.setReadChannel(QProcess.StandardOutput)

    def rr():
        counts['readyRead'] += 1

    def rr_out():
        counts['out'] += 1
        chunks['out'].append(bytes(proc.readAllStandardOutput()))

    def rr_err():
        counts['err'] += 1
        chunks['err'].append(bytes(proc.readAllStandardError()))

    proc.readyRead.connect(rr)
    proc.readyReadStandardOutput.connect(rr_out)
    proc.readyReadStandardError.connect(rr_err)

    def fin(c, s):
        print(label, "counts:", counts, "chunks:", chunks, "remaining_out:", bytes(proc.readAllStandardOutput()), "remaining_err:", bytes(proc.readAllStandardError()), flush=True)
        next_case()

    proc.finished.connect(fin)
    proc.start(sys.executable, ['-c', script])


cases = [
    ("stdout single print", "import sys; print('stdout text')"),
    ("stderr single write", r"import sys; sys.stderr.write('stderr text\n')"),
    ("both", "import sys; print('out'); sys.stderr.write('err\n')"),
    ("two chunks", "import sys, time; print('a', flush=True, end=''); time.sleep(0.3); print('b', flush=True, end='')"),
]

idx = {'i': 0}


def next_case():
    i = idx['i']
    if i >= len(cases):
        app.quit()
        return
    idx['i'] += 1
    label, script = cases[i]
    run(script, label)


next_case()
app.exec_()
