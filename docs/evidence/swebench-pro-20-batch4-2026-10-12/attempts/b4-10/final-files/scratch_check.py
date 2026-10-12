from qutebrowser.qt.core import Qt
from qutebrowser.qt.gui import QKeySequence

mask = Qt.KeyboardModifier.KeyboardModifierMask
print("mask", mask, "notmask", ~mask)
for v in [-1, -33554432, 0x0c000000, 0x04000000]:
    try:
        m = Qt.KeyboardModifiers(v)
        print("KeyboardModifiers ok", v, int(m))
    except Exception as e:
        print("KeyboardModifiers ERR", v, type(e).__name__, e)
    try:
        m2 = Qt.KeyboardModifier(v)
        print("KeyboardModifier ok", v, int(m2))
    except Exception as e:
        print("KeyboardModifier ERR", v, type(e).__name__, e)

for comb in [-1, 33554431]:
    key = Qt.Key(comb & ~mask)
    mods = Qt.KeyboardModifiers(comb & mask)
    print("from int", comb, "->", key, mods)

# round trip check
info = (Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
val = int(info[0]) | int(info[1])
print("roundtrip", list(QKeySequence(val)), QKeySequence(val))
print("parsed", list(QKeySequence('ctrl+alt+y')))
print("eq", QKeySequence('ctrl+alt+y') == QKeySequence(int(Qt.Key.Key_Y) | 0x0c000000))
print("multi eq", QKeySequence(88, 89, 90) == QKeySequence('x, y, z'))
print("neg seq", QKeySequence(-1), bool(QKeySequence(-1)), len(QKeySequence(-1)))
print("zero seq", QKeySequence(0), bool(QKeySequence(0)))
