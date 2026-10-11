"""Standalone arithmetic probe for hue/component scaling (no qutebrowser import)."""

def parse(val, max_val, mode):
    try:
        return int(val)
    except ValueError:
        pass
    mult = float(max_val)
    if val.endswith('%'):
        val = val[:-1]
        mult = max_val / 100
    f = float(val) * mult
    if mode == 'int':
        return int(f)
    return round(f)


for v, mx in [('100%', 359), ('10%', 359), ('100%', 255), ('10%', 255),
              ('20%', 255), ('30%', 255), ('40%', 255), ('50%', 255),
              ('1.0', 255), ('0.5', 255)]:
    print(v, mx, 'int=', parse(v, mx, 'int'), 'round=', parse(v, mx, 'round'),
          'raw=', repr(float(v[:-1]) * (mx / 100) if v.endswith('%')
                       else float(v) * float(mx)))
