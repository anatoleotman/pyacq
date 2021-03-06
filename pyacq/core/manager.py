import atexit

from .rpc import RPCServer, RPCClientSocket, RPCClient
from .client import ManagerProxy
from .processspawner import ProcessSpawner
from .host import Host

import logging

def create_manager(mode='rpc', auto_close_at_exit = True):
    """Create a new Manager either in this process or in a new process.
    
    Parameters
    ----------
    auto_close_at_exit : bool
        call close automatiqcally if the programme exit.
    """
    if mode == 'local':
        return Manager(name='manager', addr='tcp://*:*')
    else:
        proc = ProcessSpawner(Manager, name='manager', addr='tcp://127.0.0.1:*')
        man = ManagerProxy(proc.name, proc.addr, manager_process = proc)
        if auto_close_at_exit:
            atexit.register(man.close)
        return man
        

class Manager(RPCServer):
    """Manager is a central point of control for connecting to hosts, creating
    Nodegroups and Nodes, and interacting with Nodes.
    
    It can either be instantiated directly or in a subprocess and accessed
    remotely by RPC::
    
        mgr_proc = ProcessSpawner(Manager, name='manager', addr='tcp://127.0.0.1:*')
        mgr = RPCClient(mgr_proc.name, mgr_proc.addr)
        
       
    Parameters
    ----------
    name : str
        A unique identifier for this manager.
    addr : str
        The address for the manager's RPC server.
    """
    
    # Classes used internally for bookkeeping
    class _Host(object):
        def __init__(self, name, addr):
            self.rpc_address = addr
            self.rpc_name = name
            self.client = RPCClient(name, addr)
            self.nodegroups = {}
            self.rpc_hostname = addr.partition('//')[2].rpartition(':')[0]

        def add_nodegroup(self, ng):
            self.nodegroups[ng.rpc_name] = ng
        
        def list_nodegroups(self):
            return list(self.nodegroups.keys())


    class _NodeGroup(object):
        def __init__(self, host, name, addr):
            self.host = host
            self.rpc_address = addr
            self.rpc_name = name
            self.client = RPCClient(name, addr)
            self.nodes = {}

        def add_node(self, name, node):
            self.nodes[name] = node

        def list_nodes(self):
            return list(self.nodes.keys())

        def delete_node(self, name):
            del self.nodes[name]
        
            
    class _Node(object):
        def __init__(self, nodegroup, name, classname):
            self.nodegroup = nodegroup
            self.name = name
            self.classname = classname
            self.outputs = [ ]# list of StreamDef
    
    
    def __init__(self, name, addr, manager_process = None):
        RPCServer.__init__(self, name, addr)
        
        self.hosts = {}  # name:HostProxy
        self.nodegroups = {}  # name:NodegroupProxy
        self.nodes = {}  # name:NodeProxy
        
        # auto-generated host on the local machine
        self._default_host = None
        
        # for auto-generated node / nodegroup names
        self._next_nodegroup_name = 0
        self._next_node_name = 0
        
        # shared socket for all RPC client connections
        self._rpc_socket = RPCClientSocket()
    
    def connect_host(self, name, addr):
        """Connect the manager to a Host.
        
        Hosts are used as a stable service on remote machines from which new
        Nodegroups can be spawned or closed.
        """
        if name not in self.hosts:
            hp = Manager._Host(name, addr)
            self.hosts[name] = hp

    def disconnect_host(self, name):
        """Disconnect the Manager from the Host identified by *name*.
        """
        for ng in self.hosts[name]:
            self.nodegroups.pop(ng.name)
        self.hosts.pop(name)
    
    def default_host(self):
        """Return the RPC name and address of a default Host created by the
        Manager.
        """
        if self._default_host is None:
            addr = self._addr.rpartition(b':')[0] + b':*'
            proc = ProcessSpawner(Host, name='default-host', addr=addr)
            self._default_host = proc
            self.connect_host(proc.name, proc.addr)
        return self._default_host.name, self._default_host.addr
    
    def close_host(self, name):
        """Close the Host identified by *name*.
        """
        self.hosts[name].client.close()
    
    def close(self):
        """
        Close the manager
        And close the default Host too.
        """
        if self._default_host is not None:
            self._default_host.stop()
        RPCServer.close(self)

    def list_hosts(self):
        """Return a list of the identifiers for Hosts that the Manager is
        connected to.
        """
        return list(self.hosts.keys())
    
    def create_nodegroup(self, host, name):
        """Create a new Nodegroup.
        
        Parameters
        ----------
        host : str
            The identifier of the Host that should be used to spawn the new
            Nodegroup.
        name : str
            A unique identifier for the new Nodegroup.
        """
        if name in self.nodegroups:
            raise KeyError("Nodegroup named %s already exists" % name)
        host = self.hosts[host]
        addr = 'tcp://%s:*' % (host.rpc_hostname)
        _, addr = host.client.create_nodegroup(name, addr)
        ng = Manager._NodeGroup(host, name, addr)
        host.add_nodegroup(ng)
        self.nodegroups[name] = ng
        return name, addr
    
    #~ def close_nodegroup(self, name):
        #~ self.nodegroups[name].host.client.close_nodegroup(name)

    def list_nodegroups(self, host=None):
        if host is None:
            return list(self.nodegroups.keys())
        else:
            return self.hosts[host].list_nodegroups()

    def create_node(self, nodegroup, name, classname, **kwargs):
        if name in self.nodes:
            raise KeyError("Node named %s already exists" % name)
        ng = self.nodegroups[nodegroup]
        ng.client.create_node(name, classname, **kwargs)
        node = Manager._Node(ng, name, classname)
        self.nodes[name] = node
        ng.add_node(name, node)

    def list_nodes(self, nodegroup=None):
        if nodegroup is None:
            return list(self.nodes.keys())
        else:
            return self.nodegroups[nodegroup].list_nodes()

    def control_node(self, name, method, **kwargs):
        ng = self.nodes[name].nodegroup
        return ng.client.control_node(name, method, **kwargs)
    
    def delete_node(self, name):
        ng = self.nodes[name].nodegroup
        ng.client.delete_node(name)
        del self.nodes[name]
        ng.delete_node(name)

    def suggest_nodegroup_name(self):
        name = 'nodegroup-%d' % self._next_nodegroup_name
        self._next_nodegroup_name += 1
        return name
    
    def suggest_node_name(self):
        name = 'node-%d' % self._next_node_name
        self._next_node_name += 1
        return name
    
    def start_all_nodes(self):
        for ng in self.nodegroups.values():
            ng.client.start_all_nodes()
    
    def stop_all_nodes(self):
        for ng in self.nodegroups.values():
            ng.client.stop_all_nodes()

