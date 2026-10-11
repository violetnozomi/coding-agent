from qutebrowser.config.configtypes import QtColor
from PyQt5.QtGui import QColor

t = QtColor()
for val in ['hsv(100%,100%,100%)', 'hsv(10%,10%,10%)', 'hsva(10%,20%,30%,40%)']:
    got = t.to_py(val)
    print(val, '->', got.isValid(), got.getHsv(), got.getRgb())

print('parse 100% @359', t._parse_value('100%', 359))
print('parse 10% @359', t._parse_value('10%', 359))
print('parse 100% @255', t._parse_value('100%', 255))
print('parse 10% @255', t._parse_value('10%', 255))
print('parse 20% @255', t._parse_value('20%', 255))
print('parse 30% @255', t._parse_value('30%', 255))
print('parse 40% @255', t._parse_value('40%', 255))
print('exp fromHsv(359,255,255)', QColor.fromHsv(359, 255, 255).getHsv())
print('eq', t.to_py('hsv(100%,100%,100%)') == QColor.fromHsv(359, 255, 255))
print('eq35', t.to_py('hsv(10%,10%,10%)') == QColor.fromHsv(35, 25, 25))
print('eq hsva', t.to_py('hsva(10%,20%,30%,40%)') == QColor.fromHsv(35, 51, 76, 102))
