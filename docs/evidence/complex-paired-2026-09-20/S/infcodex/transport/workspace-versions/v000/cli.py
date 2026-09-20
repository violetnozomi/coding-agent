import store
def update(path, key, value):
    data = store.load(path)
    data[key] = value
    store.save(path, data)
    return data
