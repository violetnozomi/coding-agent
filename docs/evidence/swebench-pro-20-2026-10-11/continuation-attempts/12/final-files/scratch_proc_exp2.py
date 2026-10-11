import sys
from PyQt5.QtCore import QCoreApplication, QProcess, QTimer

app = QCoreApplication(sys.argv)

results = []
N = 30
cases = [
    ('stdout', 'import sys; print("stdout text")'),
    ('stderr', 'import sys; sys.stderr.write("stderr text\\n")'),
    ('both', 'import sys; print("stdout text"); sys.stderr.write("stderr text\\n")'),
]


def run(script, label):
    proc = QProcess()
    proc.setReadChannel(QProcess.StandardOutput)
    counts = {'out': 0, 'err': 0}
    proc.readyReadStandardOutput.connect(lambda: counts.__setitem__('out', counts['out'] + 1))
    proc.readyReadStandardError.connect(lambda: counts.__setitem__('err', counts['err'] + 1))

    def fin(c, s):
        results.append((label, counts['out'], counts['err']))
        step()

    proc.finished.connect(fin)
    proc.start(sys.executable, ['-c', script])
    proc.closeWriteChannel()


idx = {'i': 0}


def step():
    i = idx['i']
    if i >= N * len(cases):
        dump()
        return
    idx['i'] += 1
    label, script = cases[i % len(cases)]
    run(script, label)


def dump():
    from collections import Counter
    print(Counter(results))
    app.quit()


QTimer.singleShot(0, step)
app.exec_()
