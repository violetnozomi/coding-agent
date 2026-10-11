from PyQt5.QtGui import QColor

from qutebrowser.config import configtypes


def main():
    t = configtypes.QtColor()
    for v in ['100%', '10%', '20%', '30%', '40%']:
        print(v, 'hue359=', repr(t._parse_value(v, 359)),
              'hue255=', repr(t._parse_value(v, 255)))
    cases = [
        ('hsv(100%,100%,100%)', QColor.fromHsv(359, 255, 255)),
        ('hsv(10%,10%,10%)', QColor.fromHsv(36, 25, 25)),
        ('hsv(10%,10%,10%)', QColor.fromHsv(35, 25, 25)),
        ('hsva(10%,20%,30%,40%)', QColor.fromHsv(36, 51, 76, 102)),
        ('hsva(10%,20%,30%,40%)', QColor.fromHsv(35, 51, 76, 102)),
    ]
    for val, expected in cases:
        got = t.to_py(val)
        print(val, 'got', got.getHsv(), got.getRgb(),
              '| exp', expected.getHsv(), expected.getRgb(),
              '| eq', got == expected)


if __name__ == '__main__':
    main()
