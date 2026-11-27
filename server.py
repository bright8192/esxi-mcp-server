import os
import json
import logging
import ssl
import argparse
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any

# MCP protocol related imports
from mcp.server.lowlevel import Server  # MCP server base class
from mcp.server.sse import SseServerTransport  # SSE transport support
from mcp import types  # MCP type definitions

# pyVmomi VMware API imports
from pyVim import connect
from pyVmomi import vim, vmodl

# Configuration data class for storing configuration options
@dataclass
class Config:
    vcenter_host: str
    vcenter_user: str
    vcenter_password: str
    datacenter: Optional[str] = None   # Datacenter name (optional)
    cluster: Optional[str] = None      # Cluster name (optional)
    datastore: Optional[str] = None    # Datastore name (optional)
    network: Optional[str] = None      # Virtual network name (optional)
    insecure: bool = False             # Whether to skip SSL certificate verification (default: False)
    api_key: Optional[str] = None      # API access key for authentication
    log_file: Optional[str] = None     # Log file path (if not specified, output to console)
    log_level: str = "INFO"            # Log level

# VMware management class, encapsulating pyVmomi operations for vSphere
class VMwareManager:
    def __init__(self, config: Config):
        self.config = config
        self.si = None               # Service instance (ServiceInstance)
        self.content = None          # vSphere content root
        self.datacenter_obj = None
        self.resource_pool = None
        self.datastore_obj = None
        self.network_obj = None
        self.authenticated = False   # Authentication flag for API key verification
        self._connect_vcenter()

    def _connect_vcenter(self):
        """Connect to vCenter/ESXi and retrieve main resource object references."""
        try:
            if self.config.insecure:
                # Connection method without SSL certificate verification
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False  # Disable hostname checking
                context.verify_mode = ssl.CERT_NONE
                self.si = connect.SmartConnect(
                    host=self.config.vcenter_host,
                    user=self.config.vcenter_user,
                    pwd=self.config.vcenter_password,
                    sslContext=context)
            else:
                # Standard SSL verification connection
                self.si = connect.SmartConnect(
                    host=self.config.vcenter_host,
                    user=self.config.vcenter_user,
                    pwd=self.config.vcenter_password)
        except Exception as e:
            logging.error(f"Failed to connect to vCenter/ESXi: {e}")
            raise
        # Retrieve content root object
        self.content = self.si.RetrieveContent()
        logging.info("Successfully connected to VMware vCenter/ESXi API")

        # Retrieve target datacenter object
        if self.config.datacenter:
            # Find specified datacenter by name
            self.datacenter_obj = next((dc for dc in self.content.rootFolder.childEntity
                                        if isinstance(dc, vim.Datacenter) and dc.name == self.config.datacenter), None)
            if not self.datacenter_obj:
                logging.error(f"Datacenter named {self.config.datacenter} not found")
                raise Exception(f"Datacenter {self.config.datacenter} not found")
        else:
            # Default to the first available datacenter
            self.datacenter_obj = next((dc for dc in self.content.rootFolder.childEntity
                                        if isinstance(dc, vim.Datacenter)), None)
        if not self.datacenter_obj:
            raise Exception("No datacenter object found")

        # Retrieve resource pool (if a cluster is configured, use the cluster's resource pool; otherwise, use the host resource pool)
        compute_resource = None
        if self.config.cluster:
            # Find specified cluster
            for folder in self.datacenter_obj.hostFolder.childEntity:
                if isinstance(folder, vim.ClusterComputeResource) and folder.name == self.config.cluster:
                    compute_resource = folder
                    break
            if not compute_resource:
                logging.error(f"Cluster named {self.config.cluster} not found")
                raise Exception(f"Cluster {self.config.cluster} not found")
        else:
            # Default to the first ComputeResource (cluster or standalone host)
            compute_resource = next((cr for cr in self.datacenter_obj.hostFolder.childEntity
                                      if isinstance(cr, vim.ComputeResource)), None)
        if not compute_resource:
            raise Exception("No compute resource (cluster or host) found")
        self.resource_pool = compute_resource.resourcePool
        logging.info(f"Using resource pool: {self.resource_pool.name}")

        # Retrieve datastore object
        if self.config.datastore:
            # Find specified datastore in the datacenter
            self.datastore_obj = next((ds for ds in self.datacenter_obj.datastoreFolder.childEntity
                                       if isinstance(ds, vim.Datastore) and ds.name == self.config.datastore), None)
            if not self.datastore_obj:
                logging.error(f"Datastore named {self.config.datastore} not found")
                raise Exception(f"Datastore {self.config.datastore} not found")
        else:
            # Default to the datastore with the largest available capacity
            datastores = [ds for ds in self.datacenter_obj.datastoreFolder.childEntity if isinstance(ds, vim.Datastore)]
            if not datastores:
                raise Exception("No available datastore found in the datacenter")
            # Select the one with the maximum free space
            self.datastore_obj = max(datastores, key=lambda ds: ds.summary.freeSpace)
        logging.info(f"Using datastore: {self.datastore_obj.name}")

        # Retrieve network object (network or distributed virtual portgroup)
        if self.config.network:
            # Find specified network in the datacenter network list
            networks = self.datacenter_obj.networkFolder.childEntity
            self.network_obj = next((net for net in networks if net.name == self.config.network), None)
            if not self.network_obj:
                logging.error(f"Network {self.config.network} not found")
                raise Exception(f"Network {self.config.network} not found")
            logging.info(f"Using network: {self.network_obj.name}")
        else:
            self.network_obj = None  # If no network is specified, VM creation can choose to not connect to a network

    def list_vms(self) -> list:
        """List all virtual machine names."""
        vm_list = []
        # Create a view to iterate over all virtual machines
        container = self.content.viewManager.CreateContainerView(self.content.rootFolder, [vim.VirtualMachine], True)
        for vm in container.view:
            vm_list.append(vm.name)
        container.Destroy()
        return vm_list

    def find_vm(self, name: str) -> Optional[vim.VirtualMachine]:
        """Find virtual machine object by name."""
        container = self.content.viewManager.CreateContainerView(self.content.rootFolder, [vim.VirtualMachine], True)
        vm_obj = None
        for vm in container.view:
            if vm.name == name:
                vm_obj = vm
                break
        container.Destroy()
        return vm_obj

    def get_vm_performance(self, vm_name: str) -> Dict[str, Any]:
        """Retrieve performance data (CPU, memory, storage, and network) for the specified virtual machine."""
        vm = self.find_vm(vm_name)
        if not vm:
            raise Exception(f"VM {vm_name} not found")
        # CPU and memory usage (obtained from quickStats)
        stats = {}
        qs = vm.summary.quickStats
        stats["cpu_usage"] = qs.overallCpuUsage  # MHz
        stats["memory_usage"] = qs.guestMemoryUsage  # MB
        # Storage usage (committed storage, in GB)
        committed = vm.summary.storage.committed if vm.summary.storage else 0
        stats["storage_usage"] = round(committed / (1024**3), 2)  # Convert to GB
        # Network usage (obtained from host or VM NIC statistics, latest sample)
        # Here we simply obtain the latest performance counter for VM network I/O
        net_bytes_transmitted = 0
        net_bytes_received = 0
        try:
            pm = self.content.perfManager
            # Define performance counter IDs to query: network transmitted and received bytes
            counter_ids = []
            for c in pm.perfCounter:
                counter_full_name = f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}"
                if counter_full_name in ("net.transmitted.average", "net.received.average"):
                    counter_ids.append(c.key)
            if counter_ids:
                query = vim.PerformanceManager.QuerySpec(maxSample=1, entity=vm, metricId=[vim.PerformanceManager.MetricId(counterId=cid, instance="*") for cid in counter_ids])
                stats_res = pm.QueryStats(querySpec=[query])
                for series in stats_res[0].value:
                    # Sum data from each network interface
                    if series.id.counterId == counter_ids[0]:
                        net_bytes_transmitted = sum(series.value)
                    elif series.id.counterId == counter_ids[1]:
                        net_bytes_received = sum(series.value)
            stats["network_transmit_KBps"] = net_bytes_transmitted
            stats["network_receive_KBps"] = net_bytes_received
        except Exception as e:
            # If obtaining performance counters fails, log the error but do not terminate
            logging.warning(f"Failed to retrieve network performance data: {e}")
            stats["network_transmit_KBps"] = None
            stats["network_receive_KBps"] = None
        return stats

    def create_vm(self, name: str, cpus: int, memory_mb: int, datastore: Optional[str] = None, network: Optional[str] = None) -> str:
        """Create a new virtual machine (from scratch, with an empty disk and optional network)."""
        # If a specific datastore or network is provided, update the corresponding object accordingly
        datastore_obj = self.datastore_obj
        network_obj = self.network_obj
        if datastore:
            datastore_obj = next((ds for ds in self.datacenter_obj.datastoreFolder.childEntity
                                   if isinstance(ds, vim.Datastore) and ds.name == datastore), None)
            if not datastore_obj:
                raise Exception(f"Specified datastore {datastore} not found")
        if network:
            networks = self.datacenter_obj.networkFolder.childEntity
            network_obj = next((net for net in networks if net.name == network), None)
            if not network_obj:
                raise Exception(f"Specified network {network} not found")

        # Build VM configuration specification
        vm_spec = vim.vm.ConfigSpec(name=name, memoryMB=memory_mb, numCPUs=cpus, guestId="otherGuest")  # guestId can be adjusted as needed
        device_specs = []

        # Add SCSI controller
        controller_spec = vim.vm.device.VirtualDeviceSpec()
        controller_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        controller_spec.device = vim.vm.device.ParaVirtualSCSIController()  # Using ParaVirtual SCSI controller
        controller_spec.device.deviceInfo = vim.Description(label="SCSI Controller", summary="ParaVirtual SCSI Controller")
        controller_spec.device.busNumber = 0
        controller_spec.device.sharedBus = vim.vm.device.VirtualSCSIController.Sharing.noSharing
        # Set a temporary negative key for the controller for later reference
        controller_spec.device.key = -101
        device_specs.append(controller_spec)

        # Add virtual disk
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.create
        disk_spec.device = vim.vm.device.VirtualDisk()
        disk_spec.device.capacityInKB = 1024 * 1024 * 10  # Create a 10GB disk
        disk_spec.device.deviceInfo = vim.Description(label="Hard Disk 1", summary="10 GB disk")
        disk_spec.device.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        disk_spec.device.backing.diskMode = "persistent"
        disk_spec.device.backing.thinProvisioned = True  # Thin provisioning
        disk_spec.device.backing.datastore = datastore_obj
        # Attach the disk to the previously created controller
        disk_spec.device.controllerKey = controller_spec.device.key
        disk_spec.device.unitNumber = 0
        device_specs.append(disk_spec)

        # If a network is provided, add a virtual network adapter
        if network_obj:
            nic_spec = vim.vm.device.VirtualDeviceSpec()
            nic_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
            nic_spec.device = vim.vm.device.VirtualVmxnet3()  # Using VMXNET3 network adapter
            nic_spec.device.deviceInfo = vim.Description(label="Network Adapter 1", summary=network_obj.name)
            if isinstance(network_obj, vim.Network):
                nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(network=network_obj, deviceName=network_obj.name)
            elif isinstance(network_obj, vim.dvs.DistributedVirtualPortgroup):
                # Distributed virtual switch portgroup
                dvs_uuid = network_obj.config.distributedVirtualSwitch.uuid
                port_key = network_obj.key
                nic_spec.device.backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                    port=vim.dvs.PortConnection(portgroupKey=port_key, switchUuid=dvs_uuid)
                )
            nic_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True)
            device_specs.append(nic_spec)

        vm_spec.deviceChange = device_specs

        # Get the folder in which to place the VM (default is the datacenter's vmFolder)
        vm_folder = self.datacenter_obj.vmFolder
        # Create the VM in the specified resource pool
        try:
            task = vm_folder.CreateVM_Task(config=vm_spec, pool=self.resource_pool)
            # Wait for the task to complete
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                continue
            if task.info.state == vim.TaskInfo.State.error:
                raise task.info.error
        except Exception as e:
            logging.error(f"Failed to create virtual machine: {e}")
            raise
        logging.info(f"Virtual machine created: {name}")
        return f"VM '{name}' created."

    def clone_vm(self, template_name: str, new_name: str) -> str:
        """Clone a new virtual machine from an existing template or VM."""
        template_vm = self.find_vm(template_name)
        if not template_vm:
            raise Exception(f"Template virtual machine {template_name} not found")
        vm_folder = template_vm.parent  # Place the new VM in the same folder as the template
        if not isinstance(vm_folder, vim.Folder):
            vm_folder = self.datacenter_obj.vmFolder
        # Keep the template's own disk datastore (thin clone); fall back to config.
        _ds = None
        for _d in template_vm.config.hardware.device:
            if isinstance(_d, vim.vm.device.VirtualDisk) and getattr(_d.backing, "datastore", None):
                _ds = _d.backing.datastore
                break
        if _ds is None:
            _ds = self.datastore_obj
        # Pin a host that has ALL the template's networks AND the datastore, so the
        # clone is not placed on an incompatible host ("No host is compatible ...").
        _src_nets = set(n.name for n in template_vm.network)
        def _compatible(h):
            if h.runtime.connectionState != "connected" or h.runtime.inMaintenanceMode:
                return False
            return _src_nets.issubset(set(n.name for n in h.network)) and _ds.name in set(x.name for x in h.datastore)
        _hosts = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.HostSystem], True).view
        _cur = template_vm.runtime.host
        _host = _cur if (_cur and _compatible(_cur)) else next((h for h in _hosts if _compatible(h)), None)
        if _host is None:
            raise Exception(f"No compatible host for networks {sorted(_src_nets)} + datastore {_ds.name}")
        logging.info(f"Cloning {template_name} -> {new_name} on compatible host {_host.name}")
        relocate_spec = vim.vm.RelocateSpec(host=_host, pool=_host.parent.resourcePool, datastore=_ds)
        clone_spec = vim.vm.CloneSpec(powerOn=False, template=False, location=relocate_spec)
        task = template_vm.Clone(folder=vm_folder, name=new_name, spec=clone_spec)
        logging.info(f"Clone submitted: {template_name} -> {new_name} on host {_host.name} (task {task._moId})")
        return {"status": "submitted", "new_name": new_name, "source": template_name,
                "host": _host.name, "task_id": task._moId,
                "note": "Clone runs in vCenter (minutes); the VM appears at once but is NOT ready until taskStatus(task_id) reports state 'success'."}

    def delete_vm(self, name: str) -> str:
        """Delete the specified virtual machine."""
        vm = self.find_vm(name)
        if not vm:
            raise Exception(f"Virtual machine {name} not found")
        try:
            task = vm.Destroy_Task()
            while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
                continue
            if task.info.state == vim.TaskInfo.State.error:
                raise task.info.error
        except Exception as e:
            logging.error(f"Failed to delete virtual machine: {e}")
            raise
        logging.info(f"Virtual machine deleted: {name}")
        return f"VM '{name}' deleted."

    def power_on_vm(self, name: str) -> str:
        """Power on the specified virtual machine."""
        vm = self.find_vm(name)
        if not vm:
            raise Exception(f"Virtual machine {name} not found")
        if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn:
            return f"VM '{name}' is already powered on."
        # If the VM's current host can't satisfy its networks + datastores, relocate
        # the power-on onto a compatible host (avoids "No host is compatible ...").
        _nets = set(n.name for n in vm.network)
        _dss = set(d.name for d in vm.datastore)
        def _compatible(h):
            if h.runtime.connectionState != "connected" or h.runtime.inMaintenanceMode:
                return False
            return _nets.issubset(set(n.name for n in h.network)) and _dss.issubset(set(x.name for x in h.datastore))
        _cur = vm.runtime.host
        _host = None
        if not (_cur and _compatible(_cur)):
            _hosts = self.content.viewManager.CreateContainerView(
                self.content.rootFolder, [vim.HostSystem], True).view
            _host = next((h for h in _hosts if _compatible(h)), None)
            if _host is None:
                raise Exception(f"No compatible host for '{name}' (networks {sorted(_nets)})")
        task = vm.PowerOnVM_Task(host=_host) if _host else vm.PowerOnVM_Task()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            time.sleep(1)
        if task.info.state == vim.TaskInfo.State.error:
            raise task.info.error
        logging.info(f"Virtual machine powered on: {name}" + (f" on host {_host.name}" if _host else ""))
        return f"VM '{name}' powered on." + (f" (relocated to {_host.name})" if _host else "")

    def power_off_vm(self, name: str) -> str:
        """Power off the specified virtual machine."""
        vm = self.find_vm(name)
        if not vm:
            raise Exception(f"Virtual machine {name} not found")
        if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOff:
            return f"VM '{name}' is already powered off."
        task = vm.PowerOffVM_Task()
        while task.info.state not in [vim.TaskInfo.State.success, vim.TaskInfo.State.error]:
            continue
        if task.info.state == vim.TaskInfo.State.error:
            raise task.info.error
        logging.info(f"Virtual machine powered off: {name}")
        return f"VM '{name}' powered off."

# ---------------- MCP Server Definition ----------------

# Initialize MCP Server object
mcp_server = Server(name="VMware-MCP-Server", version="0.0.1")
# Define supported tools (executable operations) and resources (data interfaces)
# The implementation of tools and resources will call methods in VMwareManager
# Note: For each operation, perform API key authentication check, and only execute sensitive operations if the authenticated flag is True
# If not authenticated, an exception is raised

# Tool 1: Authentication (via API Key)
def tool_authenticate(key: str) -> str:
    """Validate the API key and enable subsequent operations upon success."""
    if config.api_key and key == config.api_key:
        manager.authenticated = True
        logging.info("API key verification successful, client is authorized")
        return "Authentication successful."
    else:
        logging.warning("API key verification failed")
        raise Exception("Authentication failed: invalid API key.")

# Tool 2: Create virtual machine
def tool_create_vm(name: str, cpu: int, memory: int, datastore: str = None, network: str = None) -> str:
    """Create a new virtual machine."""
    _check_auth()  # Check access permissions
    return manager.create_vm(name, cpu, memory, datastore, network)

# Tool 3: Clone virtual machine
def tool_clone_vm(template_name: str, new_name: str) -> str:
    """Clone a virtual machine from a template."""
    _check_auth()
    return manager.clone_vm(template_name, new_name)

# Tool 4: Delete virtual machine
def tool_delete_vm(name: str) -> str:
    """Delete the specified virtual machine."""
    _check_auth()
    return manager.delete_vm(name)

# Tool 5: Power on virtual machine
def tool_power_on(name: str) -> str:
    """Power on the specified virtual machine."""
    _check_auth()
    return manager.power_on_vm(name)

# Tool 6: Power off virtual machine
def tool_power_off(name: str) -> str:
    """Power off the specified virtual machine."""
    _check_auth()
    return manager.power_off_vm(name)

# Tool 7: List all virtual machines
def tool_list_vms() -> list:
    """Return a list of all virtual machine names."""
    _check_auth()
    return manager.list_vms()

# Resource 1: Retrieve virtual machine performance data
def resource_vm_performance(vm_name: str) -> dict:
    """Retrieve CPU, memory, storage, and network usage for the specified virtual machine."""
    _check_auth()
    return manager.get_vm_performance(vm_name)

# Internal helper: Check API access permissions
def _check_auth():
    if config.api_key:
        # If an API key is configured, require that manager.authenticated is True
        if not manager.authenticated:
            raise Exception("Unauthorized: API key required.")

# ---- Extended tools (ported from the standalone helper scripts) ----
def tool_task_status(task_id: str):
    _check_auth()
    t = vim.Task(task_id, manager.si._stub)
    i = t.info
    return {"task_id": task_id, "state": str(i.state), "progress": i.progress,
            "description": i.descriptionId, "entity": i.entityName,
            "error": (i.error.msg if i.error else None),
            "complete_time": str(i.completeTime) if i.completeTime else None}

def _all_vms():
    cv = manager.content.viewManager.CreateContainerView(manager.content.rootFolder, [vim.VirtualMachine], True)
    r = list(cv.view); cv.Destroy(); return r

def _all_hosts():
    cv = manager.content.viewManager.CreateContainerView(manager.content.rootFolder, [vim.HostSystem], True)
    r = list(cv.view); cv.Destroy(); return r

def _wait(t):
    while t.info.state not in (vim.TaskInfo.State.success, vim.TaskInfo.State.error):
        time.sleep(1)
    if t.info.state == vim.TaskInfo.State.error:
        raise Exception(getattr(t.info.error, "msg", t.info.error))

def _host_network(host, netname):
    return next((n for n in host.network if n.name == netname), None)

def tool_find_vm_by_ip(ip: str):
    _check_auth()
    hits = []
    for vm in _all_vms():
        ips = set(); g = vm.guest
        if g:
            if g.ipAddress: ips.add(g.ipAddress)
            for nic in (g.net or []):
                for a in (nic.ipAddress or []): ips.add(a)
        if ip in ips or ip in vm.name:
            hits.append({"name": vm.name, "power": str(vm.runtime.powerState),
                         "host": vm.runtime.host.name if vm.runtime.host else None,
                         "guest_ips": sorted(ips)})
    return hits or f"No VM found with guest-IP or name matching {ip} (bare-metal or VMware Tools not running won't match)."

def tool_find_vm_by_mac(mac: str):
    _check_auth()
    m = mac.lower(); hits = []
    for vm in _all_vms():
        for d in vm.config.hardware.device:
            if isinstance(d, vim.vm.device.VirtualEthernetCard) and d.macAddress and d.macAddress.lower() == m:
                hits.append({"name": vm.name, "nic_mac": d.macAddress, "source": "config",
                             "host": vm.runtime.host.name if vm.runtime.host else None})
        if vm.guest and vm.guest.net:
            for nic in vm.guest.net:
                if nic.macAddress and nic.macAddress.lower() == m:
                    hits.append({"name": vm.name, "mac": nic.macAddress, "source": "guest"})
    return hits or f"No VM in this vCenter has MAC {mac}."

def tool_vm_details(name: str):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    cfg, rt, sm = vm.config, vm.runtime, vm.summary
    disks = []; nics = []
    for d in cfg.hardware.device:
        if isinstance(d, vim.vm.device.VirtualDisk):
            disks.append({"label": d.deviceInfo.label, "gb": round(d.capacityInKB/1024/1024, 1),
                          "datastore": d.backing.datastore.name if getattr(d.backing, "datastore", None) else None,
                          "thin": getattr(d.backing, "thinProvisioned", None)})
        if isinstance(d, vim.vm.device.VirtualEthernetCard):
            b = d.backing
            net = getattr(b, "deviceName", None) or getattr(getattr(b, "port", None), "portgroupKey", None)
            nics.append({"label": d.deviceInfo.label, "mac": d.macAddress, "network": net,
                         "connected": d.connectable.connected if d.connectable else None})
    return {"name": vm.name, "power": str(rt.powerState),
            "guest_os_configured": cfg.guestFullName,
            "guest_os_running": vm.guest.guestFullName if vm.guest else None,
            "vcpu": cfg.hardware.numCPU, "cores_per_socket": cfg.hardware.numCoresPerSocket,
            "memory_mb": cfg.hardware.memoryMB, "memory_gb": round(cfg.hardware.memoryMB/1024, 1),
            "esxi_host": rt.host.name if rt.host else None, "hw_version": cfg.version, "uuid": cfg.uuid,
            "tools": vm.guest.toolsRunningStatus if vm.guest else None,
            "guest_ip": vm.guest.ipAddress if vm.guest else None,
            "annotation": (cfg.annotation or "").strip()[:300],
            "disks": disks, "total_disk_gb": round(sum(d["gb"] for d in disks), 1),
            "committed_gb": round(sm.storage.committed/1024/1024/1024, 1) if sm.storage else None,
            "nics": nics}

def tool_list_datastores(name: str):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    host = vm.runtime.host
    cur = next((d.backing.datastore.name for d in vm.config.hardware.device
                if isinstance(d, vim.vm.device.VirtualDisk) and getattr(d.backing, "datastore", None)), None)
    out = []
    for ds in sorted(host.datastore, key=lambda d: -d.summary.freeSpace):
        s = ds.summary; cap = s.capacity/1024**3; free = s.freeSpace/1024**3
        out.append({"name": s.name, "type": s.type, "cap_gb": round(cap), "free_gb": round(free),
                    "used_pct": round(100*(cap-free)/cap) if cap else 0, "current": s.name == cur})
    return {"vm": vm.name, "host": host.name, "current_datastore": cur, "datastores": out}

def tool_list_hosts():
    _check_auth()
    out = []
    for h in _all_hosts():
        hw = h.hardware
        out.append({"name": h.name, "cluster": h.parent.name if h.parent else None,
                    "connection": str(h.runtime.connectionState), "maintenance": h.runtime.inMaintenanceMode,
                    "cpu_cores": hw.cpuInfo.numCpuCores if hw else None,
                    "mem_gb": round(hw.memorySize/1024**3) if hw else None,
                    "networks": sorted(n.name for n in h.network),
                    "datastores": sorted(x.name for x in h.datastore)})
    return out

def tool_rename_vm(name: str, new_name: str):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    if vm.name == new_name: return f"VM already named '{new_name}'."
    _wait(vm.Rename_Task(new_name))
    return f"Renamed '{name}' -> '{new_name}'."

def tool_resize_vm(name: str, cpu: int, memory_gb: int):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    was_on = vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn
    if was_on:
        _wait(vm.PowerOffVM_Task())
        for _ in range(60):
            if vm.runtime.powerState == vim.VirtualMachine.PowerState.poweredOff: break
            time.sleep(1)
    spec = vim.vm.ConfigSpec(numCPUs=int(cpu), numCoresPerSocket=1, memoryMB=int(memory_gb)*1024)
    _wait(vm.ReconfigVM_Task(spec))
    msg = f"Resized '{name}' -> {cpu} vCPU / {memory_gb} GB."
    if was_on:
        try: msg += " " + manager.power_on_vm(name)
        except Exception as e: msg += f" (reconfig ok, power-on failed: {e})"
    return msg

def tool_grow_disk(name: str, new_size_gb: int):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    disk = next((d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)), None)
    if not disk: raise Exception("no virtual disk found")
    cur = disk.capacityInKB/1024/1024; newkb = int(new_size_gb)*1024*1024
    if newkb <= disk.capacityInKB:
        raise Exception(f"new size {new_size_gb}GB not larger than current {cur:.0f}GB")
    disk.capacityInKB = newkb
    ds = vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.edit, device=disk)
    _wait(vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[ds])))
    return f"Grew '{disk.deviceInfo.label}' {cur:.0f} GB -> {new_size_gb} GB (now extend the filesystem inside the guest)."

def _nic_backing(net, netname):
    if isinstance(net, vim.dvs.DistributedVirtualPortgroup):
        conn = vim.dvs.PortConnection(portgroupKey=net.key, switchUuid=net.config.distributedVirtualSwitch.uuid)
        return vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(port=conn)
    return vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(deviceName=netname, network=net)

def tool_add_nic(name: str, network: str):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    host = vm.runtime.host
    net = _host_network(host, network)
    if not net: raise Exception(f"network '{network}' not found on host {host.name}")
    nic = vim.vm.device.VirtualVmxnet3()
    nic.backing = _nic_backing(net, network)
    nic.connectable = vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, connected=True, allowGuestControl=True)
    spec = vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=nic)
    _wait(vm.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[spec])))
    return f"Added vmxnet3 NIC on '{network}' to '{name}'."

def tool_set_vm_network(name: str, network: str):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    host = vm.runtime.host
    net = _host_network(host, network)
    if not net: raise Exception(f"network '{network}' not found on host {host.name}")
    spec = vim.vm.ConfigSpec(); changed = []
    for d in vm.config.hardware.device:
        if isinstance(d, vim.vm.device.VirtualEthernetCard):
            d.backing = _nic_backing(net, network)
            d.connectable = vim.vm.device.VirtualDevice.ConnectInfo(connected=True, startConnected=True, allowGuestControl=True)
            spec.deviceChange.append(vim.vm.device.VirtualDeviceSpec(operation=vim.vm.device.VirtualDeviceSpec.Operation.edit, device=d))
            changed.append(f"{d.deviceInfo.label}({d.macAddress})")
    if not changed: raise Exception("no NIC to repoint")
    _wait(vm.ReconfigVM_Task(spec))
    return f"Repointed NIC(s) {', '.join(changed)} of '{name}' -> '{network}'."

def tool_storage_vmotion(name: str):
    _check_auth()
    vm = manager.find_vm(name)
    if not vm: raise Exception(f"VM not found: {name}")
    ds = next((d.backing.datastore for d in vm.config.hardware.device
               if isinstance(d, vim.vm.device.VirtualDisk) and getattr(d.backing, "datastore", None)), None)
    if not ds: raise Exception("no disk datastore found")
    reloc = vim.vm.RelocateSpec(); reloc.datastore = ds; locs = []
    for d in vm.config.hardware.device:
        if isinstance(d, vim.vm.device.VirtualDisk):
            back = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(); back.thinProvisioned = True; back.diskMode = d.backing.diskMode
            locs.append(vim.vm.RelocateSpec.DiskLocator(diskId=d.key, datastore=ds, diskBackingInfo=back))
    reloc.disk = locs
    task = vm.RelocateVM_Task(spec=reloc)
    return {"status": "submitted", "vm": name, "datastore": ds.name, "task_id": task._moId,
            "note": "Storage vMotion runs in vCenter (minutes). Poll with taskStatus(task_id)."}


# Register the above functions as tools and resources for the MCP Server
# Encapsulate using mcp.types.Tool and mcp.types.Resource
tools = {
    "authenticate": types.Tool(
        name="authenticate",
        description="Authenticate using API key to enable privileged operations",
        parameters={"key": str},
        handler=lambda params: tool_authenticate(**params),
        inputSchema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}
    ),
    "createVM": types.Tool(
        name="createVM",
        description="Create a new virtual machine",
        parameters={"name": str, "cpu": int, "memory": int, "datastore": Optional[str], "network": Optional[str]},
        handler=lambda params: tool_create_vm(**params),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cpu": {"type": "integer"},
                "memory": {"type": "integer"},
                "datastore": {"type": "string", "nullable": True},
                "network": {"type": "string", "nullable": True}
            },
            "required": ["name", "cpu", "memory"]
        }
    ),
    "cloneVM": types.Tool(
        name="cloneVM",
        description="Clone a VM (host-pinned). Returns immediately with a task_id; poll progress with taskStatus.",
        parameters={"template_name": str, "new_name": str},
        handler=lambda params: tool_clone_vm(**params),
        inputSchema={
            "type": "object",
            "properties": {
                "template_name": {"type": "string"},
                "new_name": {"type": "string"}
            },
            "required": ["template_name", "new_name"]
        }
    ),
    "deleteVM": types.Tool(
        name="deleteVM",
        description="Delete a virtual machine",
        parameters={"name": str},
        handler=lambda params: tool_delete_vm(**params),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"]
        }
    ),
    "powerOn": types.Tool(
        name="powerOn",
        description="Power on a virtual machine",
        parameters={"name": str},
        handler=lambda params: tool_power_on(**params),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"]
        }
    ),
    "powerOff": types.Tool(
        name="powerOff",
        description="Power off a virtual machine",
        parameters={"name": str},
        handler=lambda params: tool_power_off(**params),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"]
        }
    ),
    "listVMs": types.Tool(
        name="listVMs",
        description="List all virtual machines",
        parameters={},
        handler=lambda params: tool_list_vms(),
        inputSchema={"type": "object", "properties": {}}
    ),
    "findVmByIp": types.Tool(name="findVmByIp", description="Find VM(s) by guest IP (or IP embedded in the VM name)", inputSchema={"type":"object","properties":{"ip":{"type":"string"}},"required":["ip"]}),
    "findVmByMac": types.Tool(name="findVmByMac", description="Find VM(s) by NIC MAC address", inputSchema={"type":"object","properties":{"mac":{"type":"string"}},"required":["mac"]}),
    "vmDetails": types.Tool(name="vmDetails", description="Full details of a VM: power, OS, vCPU/RAM, host, disks, NICs (mac/network), IPs, tools", inputSchema={"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}),
    "listDatastores": types.Tool(name="listDatastores", description="Datastores available to a VM's host, with capacity/free and the VM's current datastore", inputSchema={"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}),
    "listHosts": types.Tool(name="listHosts", description="List ESXi hosts with cluster, CPU/mem, networks and datastores (for placement/compatibility)", inputSchema={"type":"object","properties":{}}),
    "renameVM": types.Tool(name="renameVM", description="Rename a VM", inputSchema={"type":"object","properties":{"name":{"type":"string"},"new_name":{"type":"string"}},"required":["name","new_name"]}),
    "resizeVM": types.Tool(name="resizeVM", description="Set vCPU and RAM(GB): powers off, reconfigures, powers back on (host-pinned)", inputSchema={"type":"object","properties":{"name":{"type":"string"},"cpu":{"type":"integer"},"memory_gb":{"type":"integer"}},"required":["name","cpu","memory_gb"]}),
    "growDisk": types.Tool(name="growDisk", description="Grow the VM's first virtual disk to new_size_gb (extend the filesystem in-guest after)", inputSchema={"type":"object","properties":{"name":{"type":"string"},"new_size_gb":{"type":"integer"}},"required":["name","new_size_gb"]}),
    "addNic": types.Tool(name="addNic", description="Add a vmxnet3 NIC on the given network (portgroup must exist on the VM's host)", inputSchema={"type":"object","properties":{"name":{"type":"string"},"network":{"type":"string"}},"required":["name","network"]}),
    "setVmNetwork": types.Tool(name="setVmNetwork", description="Repoint the VM's NIC(s) to another portgroup/network on its host", inputSchema={"type":"object","properties":{"name":{"type":"string"},"network":{"type":"string"}},"required":["name","network"]}),
    "storageVMotion": types.Tool(name="storageVMotion", description="In-place storage vMotion (thin rewrite, same datastore); returns a task_id, poll with taskStatus", inputSchema={"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}),
    "taskStatus": types.Tool(name="taskStatus", description="Check a long-running vCenter task (clone/storageVMotion) by its task_id: state, progress %, error", inputSchema={"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]})
}
resources = {
    "vmStats": types.Resource(
        name="vmStats",
        uri="vmstats://{vm_name}",
        description="Get CPU, memory, storage, network usage of a VM",
        parameters={"vm_name": str},
        handler=lambda params: resource_vm_performance(**params),
        inputSchema={
            "type": "object",
            "properties": {
                "vm_name": {"type": "string"}
            },
            "required": ["vm_name"]
        }
    )
}

# Add tools and resources to the MCP Server object
for name, tool in tools.items():
    setattr(mcp_server, f"tool_{name}", tool)
for name, res in resources.items():
    setattr(mcp_server, f"resource_{name}", res)

# Set the MCP Server capabilities, declaring that the tools and resources list is available
mcp_server.capabilities = {
    "tools": {"listChanged": True},
    "resources": {"listChanged": True}
}

# --- Real MCP handler registration ---
# The setattr()/capabilities assignment above is a no-op in the real SDK; without
# @list_tools/@call_tool handlers the client sees 0 tools. Register them here.
@mcp_server.list_tools()
async def _handle_list_tools():
    return [types.Tool(name=t.name, description=t.description, inputSchema=t.inputSchema)
            for t in tools.values()]

_TOOL_DISPATCH = {
    "authenticate": lambda a: tool_authenticate(**a),
    "createVM":     lambda a: tool_create_vm(**a),
    "cloneVM":      lambda a: tool_clone_vm(**a),
    "deleteVM":     lambda a: tool_delete_vm(**a),
    "powerOn":      lambda a: tool_power_on(**a),
    "powerOff":     lambda a: tool_power_off(**a),
    "listVMs":      lambda a: tool_list_vms(),
    "findVmByIp":     lambda a: tool_find_vm_by_ip(**a),
    "findVmByMac":    lambda a: tool_find_vm_by_mac(**a),
    "vmDetails":      lambda a: tool_vm_details(**a),
    "listDatastores": lambda a: tool_list_datastores(**a),
    "listHosts":      lambda a: tool_list_hosts(),
    "renameVM":       lambda a: tool_rename_vm(**a),
    "resizeVM":       lambda a: tool_resize_vm(**a),
    "growDisk":       lambda a: tool_grow_disk(**a),
    "addNic":         lambda a: tool_add_nic(**a),
    "setVmNetwork":   lambda a: tool_set_vm_network(**a),
    "storageVMotion": lambda a: tool_storage_vmotion(**a),
    "taskStatus":     lambda a: tool_task_status(**a),
}

@mcp_server.call_tool()
async def _handle_call_tool(name, arguments):
    arguments = arguments or {}
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, fn, arguments)
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]
    if not isinstance(result, str):
        result = json.dumps(result, default=str, indent=2)
    return [types.TextContent(type="text", text=result)]

# Maintain a global SSE transport instance for sending events during POST request processing
active_transport: Optional[SseServerTransport] = None

# SSE initialization request handler (HTTP GET /sse)
async def sse_endpoint(scope, receive, send):
    """Handle SSE connection initialization requests. Establish an MCP SSE session."""
    global active_transport
    # Construct response headers to establish an event stream
    headers = [(b"content-type", b"text/event-stream")]
    # Verify API key: Retrieve from request headers 'Authorization' or 'X-API-Key'
    headers_dict = {k.lower().decode(): v.decode() for (k, v) in scope.get("headers", [])}
    provided_key = None
    if "authorization" in headers_dict:
        provided_key = headers_dict.get("authorization")
    elif "x-api-key" in headers_dict:
        provided_key = headers_dict.get("x-api-key")
    if config.api_key and provided_key != f"Bearer {config.api_key}" and provided_key != config.api_key:
        # If the correct API key is not provided, return 401
        res_status = b"401 UNAUTHORIZED"
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})
        logging.warning("No valid API key provided, rejecting SSE connection")
        return

    # Bearer key already proved the API key; mark the shared manager authenticated
    # so tools run without a separate 'authenticate' call.
    try:
        manager.authenticated = True
    except Exception:
        pass

    # Establish SSE transport and connect to the MCP Server
    active_transport = SseServerTransport("/sse/messages")
    logging.info("Established new SSE session")
    # The mcp SDK's connect_sse() sends the ASGI response itself (SSE headers + the
    # 'endpoint' event via EventSourceResponse). Sending http.response.start here
    # caused a double-start -> TaskGroup error -> empty stream. Do NOT send it.
    try:
        async with active_transport.connect_sse(scope, receive, send) as (read_stream, write_stream):
            init_opts = mcp_server.create_initialization_options()
            # Run MCP Server, passing the read/write streams to the Server
            await mcp_server.run(read_stream, write_stream, init_opts)
    except Exception as e:
        logging.error(f"SSE session encountered an error: {e}")
    finally:
        active_transport = None
    # connect_sse()/EventSourceResponse already completed the ASGI response.

# JSON-RPC message handler (HTTP POST /sse/messages)
async def messages_endpoint(scope, receive, send):
    """Handle JSON-RPC requests sent by the client (via POST /sse/messages)."""
    global active_transport
    # Only accept requests sent through an established SSE transport
    if not active_transport:
        await send({"type": "http.response.start", "status": 400,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"No active session"})
        return
    # Delegate to the SDK ASGI app: it reads the body, routes by the session_id
    # query param into the active MCP session, and sends its own HTTP response
    # (202 Accepted). Do not pre-read the body or send a reply ourselves.
    await active_transport.handle_post_message(scope, receive, send)


# Simple ASGI application routing: dispatch requests to the appropriate handler based on the path and method
async def app(scope, receive, send):
    if scope["type"] == "http":
        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        if path == "/sse" and method == "GET":
            # SSE initialization request
            await sse_endpoint(scope, receive, send)
        elif path == "/sse/messages" and method in ("POST", "OPTIONS"):
            # JSON-RPC message request; handle CORS preflight OPTIONS request
            if method == "OPTIONS":
                # Return allowed methods
                headers = [
                    (b"access-control-allow-methods", b"POST, OPTIONS"),
                    (b"access-control-allow-headers", b"Content-Type, Authorization, X-API-Key"),
                    (b"access-control-allow-origin", b"*")
                ]
                await send({"type": "http.response.start", "status": 204, "headers": headers})
                await send({"type": "http.response.body", "body": b""})
            else:
                await messages_endpoint(scope, receive, send)
        else:
            # Route not found
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Not Found"})
    else:
        # Non-HTTP event, do not process
        return

# Parse command-line arguments and environment variables, and load configuration
parser = argparse.ArgumentParser(description="MCP VMware ESXi Management Server")
parser.add_argument("--config", "-c", help="Configuration file path (JSON or YAML)", default=None)
args = parser.parse_args()

# Attempt to load configuration from a file or environment variables
config_data = {}
config_path = args.config or os.environ.get("MCP_CONFIG_FILE")
if config_path:
    # Parse JSON or YAML based on the file extension
    if config_path.endswith((".yml", ".yaml")):
        import yaml
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)
    elif config_path.endswith(".json"):
        with open(config_path, 'r') as f:
            config_data = json.load(f)
    else:
        raise ValueError("Unsupported configuration file format. Please use JSON or YAML")
# Override configuration from environment variables (higher priority than file)
env_map = {
    "VCENTER_HOST": "vcenter_host",
    "VCENTER_USER": "vcenter_user",
    "VCENTER_PASSWORD": "vcenter_password",
    "VCENTER_DATACENTER": "datacenter",
    "VCENTER_CLUSTER": "cluster",
    "VCENTER_DATASTORE": "datastore",
    "VCENTER_NETWORK": "network",
    "VCENTER_INSECURE": "insecure",
    "MCP_API_KEY": "api_key",
    "MCP_LOG_FILE": "log_file",
    "MCP_LOG_LEVEL": "log_level"
}
for env_key, cfg_key in env_map.items():
    if env_key in os.environ:
        val = os.environ[env_key]
        # Boolean type conversion
        if cfg_key == "insecure":
            config_data[cfg_key] = val.lower() in ("1", "true", "yes")
        else:
            config_data[cfg_key] = val

# Construct Config object from config_data
required_keys = ["vcenter_host", "vcenter_user", "vcenter_password"]
for k in required_keys:
    if k not in config_data or not config_data[k]:
        raise Exception(f"Missing required configuration item: {k}")
config = Config(**config_data)

# Initialize logging
log_level = getattr(logging, config.log_level.upper(), logging.INFO)
logging.basicConfig(level=log_level,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    filename=config.log_file if config.log_file else None)
if not config.log_file:
    # If no log file is specified, output logs to the console
    logging.getLogger().addHandler(logging.StreamHandler())

logging.info("Starting VMware ESXi Management MCP Server...")
# Create VMware Manager instance and connect
manager = VMwareManager(config)

# If an API key is configured, prompt that authentication is required before invoking sensitive operations
if config.api_key:
    logging.info("API key authentication is enabled. Clients must call the authenticate tool to verify the key before invoking sensitive operations")

# Start ASGI server to listen for MCP SSE connections
if __name__ == "__main__":
    # Start ASGI application using the built-in uvicorn server (listening on 0.0.0.0:8080)
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
