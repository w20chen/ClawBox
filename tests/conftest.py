import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test-clawbox.db")
os.environ.setdefault("CONTROLLER_BACKEND", "subprocess")
os.environ.setdefault("NUMA_CAPACITY", "0:64")
