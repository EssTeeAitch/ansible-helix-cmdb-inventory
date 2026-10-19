# ansible-test units puts the collection on the import path itself.  Plain
# pytest does not, so add the directory that holds ansible_collections/.
import importlib.util
import os
import sys

COLLECTIONS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), *(['..'] * 5)))

try:
    FOUND = importlib.util.find_spec('ansible_collections.unofficial.helix_cmdb') is not None
except ImportError:
    FOUND = False
if not FOUND:
    sys.path.insert(0, COLLECTIONS_PATH)
