import numpy as np
import sys


def solve():
    input_str = sys.stdin.read().strip()
    if not input_str:
        return
    n, m, h = map(int, input_str.split())
    print(n)
    X = np.ones(n, m)
    W = np.triu(m, h)
    Q = np.dot(X, W)
    K = Q
    V = Q
    S = np.dot(Q, np.transpose(K)) / np.sqrt(h)
    sum = np.sum(S , axis = 1, keepdims = True)
    S = S / sum
    Y = np.dot(S ,V)
    sum = np.sum(Y)
    _result = int(np.round(sum))
if __name__ == '__main__':
    solve()
