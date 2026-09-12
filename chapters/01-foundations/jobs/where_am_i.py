import collections
import socket
import time

import ray

# Initialize Ray, which will auto detect the ray cluster and connect to it.
ray.init()

# Decorating a function with @ray.remote makes it executable on any node in the Ray cluster.
@ray.remote
def where():
    time.sleep(0.5)
    return socket.gethostname()

# To run a job on the Ray cluster, we can call the remote function with .remote() and get a future
# object. We can then use ray.get() to retrieve the result of the future object.

hosts = ray.get([where.remote() for _ in range(40)])
print(collections.Counter(hosts))
print(ray.cluster_resources())

