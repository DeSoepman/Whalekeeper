#!/usr/bin/env python3
"""
Test script to verify network capture and reconnection works for compose containers
"""
import docker

client = docker.from_env()

# Find a compose-managed container
compose_containers = []
for container in client.containers.list():
    if 'com.docker.compose.project' in container.labels:
        compose_containers.append(container)

if not compose_containers:
    print("❌ No compose-managed containers found")
    print("   Please start a docker-compose stack first")
    exit(1)

test_container = compose_containers[0]
print(f"✓ Found compose container: {test_container.name}")
print(f"  Project: {test_container.labels.get('com.docker.compose.project')}")
print(f"  Service: {test_container.labels.get('com.docker.compose.service')}")

# Check network configuration
network_settings = test_container.attrs.get('NetworkSettings', {})
networks = network_settings.get('Networks', {})

print(f"\n✓ Container is connected to {len(networks)} network(s):")
for network_name, network_config in networks.items():
    aliases = network_config.get('Aliases', [])
    print(f"  - {network_name}")
    print(f"    Aliases: {aliases}")

# Simulate what our get_container_config captures
captured_networks = {}
for network_name, network_config in networks.items():
    # Capture IP configuration (for static IPs in compose)
    ipam_config = network_config.get('IPAMConfig')
    ipv4_address = None
    ipv6_address = None
    
    if ipam_config:
        ipv4_address = ipam_config.get('IPv4Address')
        ipv6_address = ipam_config.get('IPv6Address')
    
    captured_networks[network_name] = {
        'aliases': network_config.get('Aliases', []),
        'links': network_config.get('Links'),
        'ipv4_address': ipv4_address,
        'ipv6_address': ipv6_address,
    }

print(f"\n✓ Captured network config:")
import json
print(json.dumps(captured_networks, indent=2))

# Check for static IPs
for network_name, network_config in captured_networks.items():
    if network_config.get('ipv4_address'):
        print(f"\n✓ Static IPv4 detected for {network_name}: {network_config['ipv4_address']}")
    if network_config.get('ipv6_address'):
        print(f"✓ Static IPv6 detected for {network_name}: {network_config['ipv6_address']}")

# Check if we have meaningful aliases (not just container IDs)
has_meaningful_aliases = False
for network_name, network_config in captured_networks.items():
    aliases = network_config.get('aliases', [])
    meaningful_aliases = []
    for alias in aliases:
        # Skip if it's the container name itself
        if alias == test_container.name:
            continue
        # Skip if it looks like a container ID (12 hex chars)
        if len(alias) == 12 and all(c in '0123456789abcdef' for c in alias):
            continue
        meaningful_aliases.append(alias)
    
    if meaningful_aliases:
        has_meaningful_aliases = True
        print(f"\n✓ Found meaningful aliases for {network_name}: {meaningful_aliases}")

if has_meaningful_aliases:
    print("\n✅ Network capture looks good! The fix should work for this container.")
else:
    print("\n⚠️  No meaningful aliases found. Container may not need special network handling.")
