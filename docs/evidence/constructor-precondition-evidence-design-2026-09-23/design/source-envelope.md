# Source envelope design

E0 (plain symbol body) is insufficient for generated constructors: it omits
decorators such as `@dataclass(frozen=True)` and can therefore hide the source
of constructor semantics. E1 is the recommended bounded envelope: contiguous
decorator lines immediately preceding the class declaration, the class header,
and the class body through its indexed end line. This preserves `__init__`,
`__post_init__`, class validators, inheritance declarations, and decorator
arguments without including the whole module.

E2 (imports plus E1) is a fallback only when a selected decorator's local
meaning cannot be represented by its source text. It should not be the normal
projection. If E1 is truncated before a validation method, the item must carry
an omission marker and should not be treated as proof-complete.

