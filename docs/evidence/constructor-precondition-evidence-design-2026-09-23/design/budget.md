# Budget proposal

For a future implementation, select at most 1–2 class/model symbols, at most
1,400 rendered characters per symbol, and at most 2,400 total characters for
this channel. Order by relation strength (direct callee, then direct
unchanged dependency), confidence descending, path and symbol ascending. Add
an explicit omission/truncation marker. A truncation before `__init__`,
`__post_init__`, or a validator makes the evidence insufficient for a
constructor proof; it remains useful context only.

