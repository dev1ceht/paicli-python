ROUTES = {"/": {"status": "running"}}


def get(path):
    return ROUTES[path], 200
