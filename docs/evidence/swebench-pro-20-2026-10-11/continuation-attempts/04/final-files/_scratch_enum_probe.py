from enum import IntEnum, IntFlag


class IteratingStates(IntEnum):
    SETUP = 0
    TASKS = 1
    RESCUE = 2
    ALWAYS = 3
    COMPLETE = 4


class FailedStates(IntFlag):
    NONE = 0
    SETUP = 1
    TASKS = 2
    RESCUE = 4
    ALWAYS = 8


print(repr(IteratingStates(0).name), str(IteratingStates(0)))
for n in (0, 1, 2, 4, 8, 3, 15, 16):
    try:
        print(n, repr(FailedStates(n).name))
    except ValueError:
        print(n, "ValueError")
print(repr(FailedStates.NONE.name))
print(IteratingStates.SETUP == 0)
print(repr(FailedStates.TASKS | FailedStates.ALWAYS))
print(repr(str(FailedStates(0))))
print(list(FailedStates))
