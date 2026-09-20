import store
def merge(path, updates):
    data = store.load(path)
    data.update(updates)
    store.save(path, data)
