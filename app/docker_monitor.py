import docker
import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime
from croniter import croniter
import time
import shlex
import os
from pathlib import Path

from app.config import Config
from app.database import Database
from app.notifications import NotificationService

logger = logging.getLogger(__name__)


class DockerMonitor:
    def __init__(self, config: Config, db: Database, notifier: NotificationService):
        self.config = config
        self.db = db
        self.notifier = notifier
        self.client = docker.from_env()
        self.running = False
        self.update_cache = {}  # Cache of containers with updates: {container_name: update_info}
    
    def _get_image_version(self, image) -> str:
        """Extract version from image labels or tags"""
        try:
            # Try to get version from image labels (check multiple standard labels)
            if image.labels:
                version_label = (
                    image.labels.get('io.hass.version') or  # Home Assistant
                    image.labels.get('org.opencontainers.image.version') or  # OCI standard
                    image.labels.get('version') or  # Generic
                    image.labels.get('VERSION')  # Generic uppercase
                )
                
                if version_label:
                    return version_label
            
            # Fall back to versioned tags (prefer versioned tags over 'latest', 'stable', 'dev')
            if image.tags:
                versioned_tags = [tag.split(':')[-1] for tag in image.tags 
                                 if not any(x in tag.lower() for x in [':latest', ':stable', ':dev'])]
                if versioned_tags:
                    return versioned_tags[0]
                
                # If only generic tags, use the first one
                tag = image.tags[0].split(':')[-1]
                if tag:
                    return tag
            
            # Return image ID as fallback
            return image.id[:12]
        except Exception:
            return image.id[:12]
    
    def _get_image_display_name(self, image) -> str:
        """Get display name for image (base name with version instead of generic tag)"""
        try:
            # Get the base image name (without tag)
            if image.tags:
                base_name = image.tags[0].rsplit(':', 1)[0]
            else:
                # No tags, return image ID
                return image.id[:12]
            
            # Get the version
            version = self._get_image_version(image)
            
            # Construct display name
            return f"{base_name}:{version}"
        except Exception:
            return image.id[:12]
    
    def has_update(self, container_name: str) -> bool:
        """Check if a container has an update available (from cache)"""
        return container_name in self.update_cache
    
    def get_monitored_containers(self) -> List[docker.models.containers.Container]:
        """Get list of containers to monitor based on configuration"""
        all_containers = self.client.containers.list()
        
        # Filter out excluded containers (if no excludes, monitor all)
        containers = [
            c for c in all_containers 
            if c.name not in self.config.monitoring.exclude_containers
        ]
        
        return containers
    
    def check_for_updates(self, container) -> Optional[Dict]:
        """Check if a newer image is available for a container"""
        try:
            # Get current image
            current_image = container.image
            image_name = container.attrs['Config']['Image']
            
            # If image_name is a sha256 digest or empty, get the actual repo name from image tags
            if not image_name or image_name.startswith('sha256:') or image_name.strip() == '':
                if current_image.tags:
                    image_name = current_image.tags[0]
                else:
                    logger.warning(f"Container {container.name} has no image tags, cannot check for updates")
                    return None
            
            # Validate image_name
            if not image_name or image_name.strip() == '':
                logger.warning(f"Container {container.name} has invalid image name, cannot check for updates")
                return None
            
            # Handle image name without tag
            if ':' not in image_name:
                image_name += ':latest'
            
            # Pull latest image
            logger.info(f"Checking for updates: {image_name}")
            
            # Login to registry if configured
            if self.config.registry.username and self.config.registry.password:
                self.client.login(
                    username=self.config.registry.username,
                    password=self.config.registry.password
                )
            
            latest_image = self.client.images.pull(image_name)
            
            # Compare image IDs
            if current_image.id != latest_image.id:
                logger.info(f"Update available for {container.name}: {current_image.id[:12]} -> {latest_image.id[:12]}")
                return {
                    'container': container,
                    'old_image': current_image,
                    'new_image': latest_image,
                    'image_name': image_name
                }
            else:
                logger.info(f"No update for {container.name}")
                # No update available - images are identical
                # Note: Docker doesn't create duplicates when pulling same image,
                # but we'll prune dangling images to keep things clean
                try:
                    self.client.images.prune(filters={'dangling': True})
                except Exception as e:
                    logger.debug(f"Image prune skipped: {e}")
                return None
                
        except Exception as e:
            logger.error(f"Error checking updates for {container.name}: {e}")
            return None
    
    def get_container_config(self, container) -> Dict:
        """Extract container configuration for recreation"""
        attrs = container.attrs
        config = attrs['Config']
        host_config = attrs['HostConfig']
        network_settings = attrs.get('NetworkSettings', {})
        
        # Capture all networks and their aliases (critical for compose containers)
        networks = {}
        network_settings_networks = network_settings.get('Networks', {})
        for network_name, network_config in network_settings_networks.items():
            # Capture IP configuration (for static IPs in compose)
            ipam_config = network_config.get('IPAMConfig')
            ipv4_address = None
            ipv6_address = None
            
            if ipam_config:
                ipv4_address = ipam_config.get('IPv4Address')
                ipv6_address = ipam_config.get('IPv6Address')
            
            networks[network_name] = {
                'aliases': network_config.get('Aliases', []),
                'links': network_config.get('Links'),
                'ipv4_address': ipv4_address,
                'ipv6_address': ipv6_address,
            }
        
        return {
            'image': attrs['Config']['Image'],
            'name': container.name,
            'environment': config.get('Env', []),
            'volumes': host_config.get('Binds', []),
            'ports': host_config.get('PortBindings', {}),
            'network_mode': host_config.get('NetworkMode', 'bridge'),
            'networks': networks,  # All networks with aliases
            'restart_policy': host_config.get('RestartPolicy', {}),
            'labels': config.get('Labels', {}),
            'command': config.get('Cmd'),
            'entrypoint': config.get('Entrypoint'),
            'working_dir': config.get('WorkingDir'),
            'user': config.get('User'),
            'hostname': config.get('Hostname'),
            'extra_hosts': host_config.get('ExtraHosts'),
            'privileged': host_config.get('Privileged', False),
            'cap_add': host_config.get('CapAdd'),
            'cap_drop': host_config.get('CapDrop'),
            'devices': host_config.get('Devices'),
        }
    
    def reconnect_networks(self, container, container_config: Dict):
        """Reconnect container to all networks with proper aliases (critical for compose)"""
        networks = container_config.get('networks', {})
        
        # Skip if no additional networks (container already on primary network from creation)
        if not networks or len(networks) <= 1:
            return
        
        # Get the network the container was created on (from network_mode)
        primary_network = container_config.get('network_mode', 'bridge')
        
        try:
            # Connect to additional networks with aliases
            for network_name, network_config in networks.items():
                # Skip the primary network (already connected during creation)
                if network_name == primary_network:
                    continue
                
                aliases = network_config.get('aliases', [])
                links = network_config.get('links')
                
                # Filter out auto-generated aliases (container ID which is 12-char hex)
                # Keep service names and other meaningful aliases
                meaningful_aliases = []
                for alias in aliases:
                    # Skip if it's the container name itself
                    if alias == container.name:
                        continue
                    # Skip if it looks like a container ID (12 hex chars)
                    if len(alias) == 12 and all(c in '0123456789abcdef' for c in alias):
                        continue
                    meaningful_aliases.append(alias)
                
                try:
                    network = self.client.networks.get(network_name)
                    
                    # Prepare connection parameters
                    connect_params = {
                        'container': container,
                        'aliases': meaningful_aliases if meaningful_aliases else None,
                        'links': links
                    }
                    
                    # Add IP addresses if they were statically assigned
                    ipv4_address = network_config.get('ipv4_address')
                    ipv6_address = network_config.get('ipv6_address')
                    
                    if ipv4_address:
                        connect_params['ipv4_address'] = ipv4_address
                    if ipv6_address:
                        connect_params['ipv6_address'] = ipv6_address
                    
                    network.connect(**connect_params)
                    
                    ip_info = f" with IP {ipv4_address}" if ipv4_address else ""
                    logger.info(f"Reconnected {container.name} to network {network_name} with aliases: {meaningful_aliases}{ip_info}")
                except docker.errors.NotFound:
                    logger.warning(f"Network {network_name} not found, skipping reconnection")
                except Exception as e:
                    logger.error(f"Failed to reconnect to network {network_name}: {e}")
        
        except Exception as e:
            logger.error(f"Error reconnecting networks for {container.name}: {e}")
    
    def _build_docker_run_command(self, config: Dict, new_image_id: str) -> str:
        """Build a docker run command from container configuration"""
        cmd_parts = ['docker run -d']
        
        # Name
        cmd_parts.append(f'--name {config["name"]}')
        
        # Environment variables
        for env in config.get('environment', []):
            cmd_parts.append(f'-e "{env}"')
        
        # Volumes
        for volume in config.get('volumes', []):
            cmd_parts.append(f'-v "{volume}"')
        
        # Ports
        for container_port, host_bindings in config.get('ports', {}).items():
            if host_bindings:
                for binding in host_bindings:
                    host_port = binding.get('HostPort')
                    if host_port:
                        cmd_parts.append(f'-p {host_port}:{container_port.split("/")[0]}')
        
        # Network mode
        network_mode = config.get('network_mode')
        if network_mode and network_mode != 'default':
            cmd_parts.append(f'--network {network_mode}')
        
        # Restart policy
        restart_policy = config.get('restart_policy', {})
        if restart_policy.get('Name'):
            policy = restart_policy['Name']
            max_retry = restart_policy.get('MaximumRetryCount', 0)
            if policy == 'on-failure' and max_retry:
                cmd_parts.append(f'--restart {policy}:{max_retry}')
            else:
                cmd_parts.append(f'--restart {policy}')
        
        # Labels
        for key, value in config.get('labels', {}).items():
            cmd_parts.append(f'--label "{key}={value}"')
        
        # Image
        cmd_parts.append(new_image_id)
        
        return ' '.join(cmd_parts)
    
    async def self_update(self, update_info: Dict) -> bool:
        """Safely update whalekeeper using a helper container"""
        try:
            container = update_info['container']
            new_image = update_info['new_image']
            old_image = update_info['old_image']
            
            logger.info(f"Initiating self-update: {old_image.id[:12]} -> {new_image.id[:12]}")
            
            # Record self-update in database before container restarts
            self.db.add_update_history(
                container_name=container.name,
                container_id=container.id,
                old_image=self._get_image_version(old_image),
                new_image=self._get_image_version(new_image),
                old_image_id=old_image.id,
                new_image_id=new_image.id,
                status="success",
                message="Self-update initiated - container will restart shortly"
            )
            
            # Get current container config
            config = self.get_container_config(container)
            
            # Use image tag instead of ID to preserve image name in recreated container
            new_image_ref = new_image.tags[0] if new_image.tags else new_image.id
            
            # Build docker run command for recreating whalekeeper
            run_cmd = self._build_docker_run_command(config, new_image_ref)
            
            # Escape command for safe shell execution
            escaped_run_cmd = shlex.quote(run_cmd)
            
            # Create helper script that will update whalekeeper
            helper_script = f'''#!/bin/sh
echo "Helper: Installing docker CLI..."
apk add --no-cache docker-cli > /dev/null 2>&1
echo "Helper: Waiting 10 seconds before updating whalekeeper..."
sleep 10
echo "Helper: Stopping whalekeeper container..."
docker stop whalekeeper || true
echo "Helper: Removing whalekeeper container..."
docker rm whalekeeper || true
echo "Helper: Starting new whalekeeper container..."
eval {escaped_run_cmd}
echo "Helper: Whalekeeper updated successfully"
'''
            
            # Run helper container with Docker socket access
            logger.info("Spawning helper container for self-update...")
            self.client.containers.run(
                image='alpine:latest',
                command=['sh', '-c', helper_script],
                volumes={'/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'}},
                remove=True,
                detach=True,
                name='whalekeeper-updater'
            )
            
            logger.info("Self-update scheduled - Whalekeeper will restart in 10 seconds")
            return True
            
        except Exception as e:
            logger.error(f"Failed to schedule self-update: {e}")
            return False
    
    async def monitor_container_health(self, container_name: str, old_image_id: str) -> tuple[bool, str]:
        """
        Monitor container health after update.
        Returns (is_healthy, failure_reason)
        
        Uses Docker HEALTHCHECK if available, otherwise monitors for crashes for 2 minutes.
        """
        try:
            container = self.client.containers.get(container_name)
            container.reload()
            
            # Check if container has a HEALTHCHECK defined
            has_healthcheck = False
            if container.attrs.get('Config', {}).get('Healthcheck'):
                has_healthcheck = True
            
            if has_healthcheck:
                # Container has HEALTHCHECK - wait for it to become healthy
                logger.info(f"Monitoring {container_name} using Docker HEALTHCHECK (max 10 minutes)")
                max_wait_time = 600  # 10 minutes max
                check_interval = 5
                elapsed = 0
                
                while elapsed < max_wait_time:
                    container.reload()
                    
                    # Check if container crashed
                    if container.status != 'running':
                        return False, f"Container stopped/crashed (status: {container.status})"
                    
                    # Check health status
                    health_status = container.attrs.get('State', {}).get('Health', {}).get('Status')
                    
                    if health_status == 'healthy':
                        logger.info(f"{container_name} is healthy after {elapsed}s")
                        return True, ""
                    elif health_status == 'unhealthy':
                        return False, "Docker health check failed (status: unhealthy)"
                    
                    # Still starting or checking, wait more
                    await asyncio.sleep(check_interval)
                    elapsed += check_interval
                
                # Timeout - health check never became healthy
                return False, f"Health check timeout after {max_wait_time}s (still in '{health_status}' state)"
            
            else:
                # No HEALTHCHECK - monitor for crashes for 2 minutes
                logger.info(f"Monitoring {container_name} for crashes (2 minutes)")
                monitoring_duration = 120  # 2 minutes
                check_interval = 10
                elapsed = 0
                initial_restart_count = container.attrs.get('RestartCount', 0)
                
                while elapsed < monitoring_duration:
                    container.reload()
                    
                    # Check if container stopped
                    if container.status != 'running':
                        return False, f"Container stopped (status: {container.status}, exit code: {container.attrs['State'].get('ExitCode', 'unknown')})"
                    
                    # Check for repeated restarts
                    current_restart_count = container.attrs.get('RestartCount', 0)
                    if current_restart_count > initial_restart_count + 1:
                        return False, f"Container restarted {current_restart_count - initial_restart_count} times"
                    
                    await asyncio.sleep(check_interval)
                    elapsed += check_interval
                
                # Made it through monitoring period without issues
                logger.info(f"{container_name} stable after {monitoring_duration}s")
                return True, ""
        
        except docker.errors.NotFound:
            return False, "Container not found"
        except Exception as e:
            logger.error(f"Error monitoring health for {container_name}: {e}")
            return False, f"Health check error: {str(e)}"
    
    async def rollback_after_failed_update(self, container_name: str, old_image_id: str, 
                                          container_config: Dict, failure_reason: str) -> bool:
        """
        Rollback to previous image after failed health check.
        """
        try:
            logger.warning(f"Auto-rollback triggered for {container_name}: {failure_reason}")
            
            # Get current (failed) container
            try:
                failed_container = self.client.containers.get(container_name)
                failed_container.stop(timeout=10)
                failed_container.remove()
            except docker.errors.NotFound:
                pass
            
            # Get old image
            old_image = self.client.images.get(old_image_id)
            
            # Recreate container with old image
            logger.info(f"Recreating {container_name} with previous image {old_image_id[:12]}")
            
            binds = container_config.get('volumes', [])
            
            new_container = self.client.containers.run(
                image=old_image.id,
                name=container_config['name'],
                environment=container_config.get('environment'),
                volumes=binds,
                ports=container_config.get('ports'),
                network_mode=container_config.get('network_mode'),
                restart_policy=container_config.get('restart_policy'),
                labels=container_config.get('labels'),
                command=container_config.get('command'),
                entrypoint=container_config.get('entrypoint'),
                working_dir=container_config.get('working_dir'),
                user=container_config.get('user'),
                hostname=container_config.get('hostname'),
                extra_hosts=container_config.get('extra_hosts'),
                privileged=container_config.get('privileged'),
                cap_add=container_config.get('cap_add'),
                cap_drop=container_config.get('cap_drop'),
                devices=container_config.get('devices'),
                detach=True
            )
            
            # Reconnect to all networks with aliases
            self.reconnect_networks(new_container, container_config)
            
            logger.info(f"Successfully rolled back {container_name} to {old_image_id[:12]}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to rollback {container_name}: {e}")
            return False
    
    async def update_container(self, update_info: Dict, send_notification: bool = True) -> bool:
        """Update a container with the new image"""
        container = update_info['container']
        old_image = update_info['old_image']
        new_image = update_info['new_image']
        image_name = update_info['image_name']
        
        # Check if this is a self-update - use helper container approach
        is_self_update = container.name == 'whalekeeper'
        
        if is_self_update:
            return await self.self_update(update_info)
        
        try:
            
            # Save current configuration for rollback
            container_config = self.get_container_config(container)
            
            # Extract version from image labels or tags (prefer version number over generic tags)
            image_tag = self._get_image_version(old_image)
            
            self.db.save_image_version(
                container_name=container.name,
                image_name=image_name,
                image_id=old_image.id,
                image_tag=image_tag,
                container_config=container_config
            )
            
            # Stop and remove old container
            logger.info(f"Stopping container {container.name}")
            container.stop(timeout=30)
            container.remove()
            
            # Create new container with same config but new image
            logger.info(f"Creating new container {container.name} with image {new_image.id[:12]}")
            
            # Use binds directly from the original container configuration
            binds = container_config.get('volumes', [])
            
            # Create new container
            new_container = self.client.containers.run(
                image=new_image.id,
                name=container_config['name'],
                environment=container_config.get('environment'),
                volumes=binds,  # Pass binds directly as list
                ports=container_config.get('ports'),
                network_mode=container_config.get('network_mode'),
                restart_policy=container_config.get('restart_policy'),
                labels=container_config.get('labels'),
                command=container_config.get('command'),
                entrypoint=container_config.get('entrypoint'),
                working_dir=container_config.get('working_dir'),
                user=container_config.get('user'),
                hostname=container_config.get('hostname'),
                extra_hosts=container_config.get('extra_hosts'),
                privileged=container_config.get('privileged'),
                cap_add=container_config.get('cap_add'),
                cap_drop=container_config.get('cap_drop'),
                devices=container_config.get('devices'),
                detach=True
            )
            
            # Reconnect to all networks with aliases (critical for compose containers)
            self.reconnect_networks(new_container, container_config)
            
            # Monitor container health after update
            logger.info(f"Monitoring {container.name} health after update...")
            is_healthy, failure_reason = await self.monitor_container_health(
                container.name, 
                old_image.id
            )
            
            if not is_healthy:
                # Health check failed - trigger auto-rollback
                logger.error(f"Health check failed for {container.name}: {failure_reason}")
                
                # Record failed update with health check info
                self.db.add_update_history(
                    container_name=container.name,
                    container_id=new_container.id,
                    old_image=self._get_image_version(old_image),
                    new_image=self._get_image_version(new_image),
                    old_image_id=old_image.id,
                    new_image_id=new_image.id,
                    status="rolled_back",
                    message=f"Auto-rollback triggered: {failure_reason}",
                    health_check_passed=False,
                    rollback_reason=failure_reason
                )
                
                # Perform rollback
                rollback_success = await self.rollback_after_failed_update(
                    container.name,
                    old_image.id,
                    container_config,
                    failure_reason
                )
                
                # Send rollback notification
                if send_notification:
                    await self.notifier.send_notification(
                        title=f"⚠️ Auto-Rollback: {container.name}",
                        message=f"Update failed health check and was automatically rolled back",
                        update_info={
                            "Container": container.name,
                            "Old Image": self._get_image_version(old_image),
                            "New Image (Failed)": self._get_image_version(new_image),
                            "Failure Reason": failure_reason,
                            "Rollback": "Success" if rollback_success else "Failed",
                            "Current State": "Running on previous version" if rollback_success else "Manual intervention required"
                        },
                        notification_type="error"
                    )
                
                return False
            
            # Health check passed!
            logger.info(f"Health check passed for {container.name}")
            
            # Record success
            self.db.add_update_history(
                container_name=container.name,
                container_id=new_container.id,
                old_image=self._get_image_version(old_image),
                new_image=self._get_image_version(new_image),
                old_image_id=old_image.id,
                new_image_id=new_image.id,
                status="success",
                message="Container updated successfully and passed health checks",
                health_check_passed=True
            )
            
            # Send notification (only for individual updates, not batch)
            if send_notification:
                await self.notifier.send_notification(
                    title=f"Container Updated: {container.name}",
                    message=f"Successfully updated container {container.name}",
                    update_info={
                        "Container": container.name,
                        "Old Image": self._get_image_version(old_image),
                        "New Image": self._get_image_version(new_image),
                        "Status": "Success ✓",
                        "Health Check": "Passed"
                    },
                    notification_type="success"
                )
            
            # Cleanup old versions
            self.db.cleanup_old_versions(
                container.name, 
                self.config.rollback.keep_versions
            )
            
            logger.info(f"Successfully updated {container.name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update {container.name}: {e}")
            
            # Record failure
            self.db.add_update_history(
                container_name=container.name,
                container_id=container.id,
                old_image=self._get_image_version(old_image),
                new_image=self._get_image_version(new_image),
                old_image_id=old_image.id,
                new_image_id=new_image.id,
                status="failed",
                message=str(e)
            )
            
            # Send failure notification (only for individual updates, not batch)
            if send_notification:
                await self.notifier.send_notification(
                    title=f"Update Failed: {container.name}",
                    message=f"Failed to update container {container.name}: {str(e)}",
                    update_info={
                        "Container": container.name,
                        "Error": str(e)
                    },
                    notification_type="error"
                )
            
            return False
    
    async def update_compose_container(self, update_info: Dict, compose_project: str, 
                                      compose_service: str, send_notification: bool = True) -> bool:
        """Update a docker-compose managed container"""
        container = update_info['container']
        old_image = update_info['old_image']
        new_image = update_info['new_image']
        image_name = update_info['image_name']
        
        try:
            logger.info(f"Updating compose-managed container {container.name} using docker-compose")
            
            # Save current configuration for potential rollback
            container_config = self.get_container_config(container)
            image_tag = self._get_image_version(old_image)
            
            self.db.save_image_version(
                container_name=container.name,
                image_name=image_name,
                image_id=old_image.id,
                image_tag=image_tag,
                container_config=container_config
            )
            
            # Get compose file path from container labels
            compose_file = container.labels.get('com.docker.compose.project.config_files')
            compose_working_dir = container.labels.get('com.docker.compose.project.working_dir')
            
            # Validate and sanitize paths to prevent command injection
            compose_file_path = None
            if compose_working_dir:
                # Validate working directory path
                try:
                    working_dir = Path(compose_working_dir).resolve()
                    # Ensure it's an absolute path and doesn't contain traversal attempts
                    if working_dir.is_absolute() and '..' not in compose_working_dir:
                        compose_file_path = str(working_dir / 'docker-compose.yml')
                    else:
                        logger.warning(f"Invalid compose working directory: {compose_working_dir}")
                except (ValueError, OSError) as e:
                    logger.warning(f"Invalid compose working directory path: {compose_working_dir}, error: {e}")
            elif compose_file:
                # Validate compose file path
                try:
                    file_path = Path(compose_file).resolve()
                    if file_path.is_absolute() and '..' not in compose_file and file_path.exists():
                        compose_file_path = str(file_path)
                    else:
                        logger.warning(f"Invalid compose file path: {compose_file}")
                except (ValueError, OSError) as e:
                    logger.warning(f"Invalid compose file path: {compose_file}, error: {e}")
            
            if not compose_file_path:
                raise Exception("No valid compose file path found in container labels")
            
            # Build docker-compose command with validated paths
            compose_cmd_parts = ['docker', 'compose', '-f', compose_file_path, '-p', compose_project, 'up', '-d', compose_service]
            
            # Execute docker-compose up command
            import subprocess
            result = subprocess.run(
                compose_cmd_parts,
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                raise Exception(f"docker-compose command failed: {result.stderr}")
            
            logger.info(f"Container {container.name} updated successfully via docker-compose")
            
            # Get the updated container
            await asyncio.sleep(2)  # Give docker-compose time to fully start the container
            try:
                new_container = self.client.containers.get(container.name)
            except docker.errors.NotFound:
                raise Exception("Container not found after docker-compose up")
            
            # Monitor container health after update
            logger.info(f"Monitoring {container.name} health after compose update...")
            is_healthy, failure_reason = await self.monitor_container_health(
                container.name, 
                old_image.id
            )
            
            if not is_healthy:
                # Health check failed - rollback to standalone container to keep service running
                logger.error(f"Health check failed for compose container {container.name}: {failure_reason}")
                logger.info(f"Auto-rolling back {container.name} to standalone container")
                
                # Record failed update with rollback info
                self.db.add_update_history(
                    container_name=container.name,
                    container_id=new_container.id,
                    old_image=self._get_image_version(old_image),
                    new_image=self._get_image_version(new_image),
                    old_image_id=old_image.id,
                    new_image_id=new_image.id,
                    status="rolled_back",
                    message=f"Compose container health check failed, rolled back to standalone: {failure_reason}",
                    health_check_passed=False,
                    rollback_reason=failure_reason
                )
                
                # Perform rollback to standalone container
                rollback_success = await self.rollback_after_failed_update(
                    container.name,
                    old_image.id,
                    container_config,
                    failure_reason
                )
                
                # Send notification with clear instructions
                if send_notification:
                    if rollback_success:
                        # Get compose working directory if available
                        compose_dir = container.labels.get('com.docker.compose.project.working_dir', '/path/to/compose/dir')
                        
                        message_detail = (
                            f"The update failed health checks and was automatically rolled back.\n\n"
                            f"Your service is running with the old version as a STANDALONE container "
                            f"(no longer managed by docker-compose).\n\n"
                            f"To restore compose management:\n"
                            f"1. Stop and remove the standalone container:\n"
                            f"   docker stop {container.name}\n"
                            f"   docker rm {container.name}\n\n"
                            f"2. Go to your compose directory and start the service:\n"
                            f"   cd {compose_dir}\n"
                            f"   docker compose up -d {compose_service}\n\n"
                            f"This will restore full compose management with networks and dependencies."
                        )
                    else:
                        message_detail = (
                            f"The update failed health checks AND automatic rollback failed.\n"
                            f"Manual intervention required immediately!"
                        )
                    
                    await self.notifier.send_notification(
                        title=f"⚠️ Auto-Rollback (Compose): {container.name}",
                        message=message_detail,
                        update_info={
                            "Container": container.name,
                            "Old Image": self._get_image_version(old_image),
                            "New Image (Failed)": self._get_image_version(new_image),
                            "Failure Reason": failure_reason,
                            "Rollback Status": "Success - Running as standalone" if rollback_success else "FAILED - Needs manual fix",
                            "Original Project": compose_project,
                            "Original Service": compose_service
                        },
                        notification_type="warning"
                    )
                
                return False
            
            # Health check passed
            logger.info(f"Health check passed for compose container {container.name}")
            
            # Record success
            self.db.add_update_history(
                container_name=container.name,
                container_id=new_container.id,
                old_image=self._get_image_version(old_image),
                new_image=self._get_image_version(new_image),
                old_image_id=old_image.id,
                new_image_id=new_image.id,
                status="success",
                message="Compose-managed container updated successfully and passed health checks",
                health_check_passed=True
            )
            
            # Send notification
            if send_notification:
                await self.notifier.send_notification(
                    title=f"Container Updated: {container.name}",
                    message=f"Successfully updated compose-managed container {container.name}",
                    update_info={
                        "Container": container.name,
                        "Old Image": self._get_image_version(old_image),
                        "New Image": self._get_image_version(new_image),
                        "Status": "Success ✓",
                        "Health Check": "Passed",
                        "Managed By": f"docker-compose (project: {compose_project})"
                    },
                    notification_type="success"
                )
            
            # Cleanup old versions
            self.db.cleanup_old_versions(
                container.name, 
                self.config.rollback.keep_versions
            )
            
            logger.info(f"Successfully updated compose container {container.name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update compose container {container.name}: {e}")
            
            # Record failure
            self.db.add_update_history(
                container_name=container.name,
                container_id=container.id,
                old_image=self._get_image_version(old_image),
                new_image=self._get_image_version(new_image),
                old_image_id=old_image.id,
                new_image_id=new_image.id,
                status="failed",
                message=f"Compose update failed: {str(e)}"
            )
            
            # Send failure notification
            if send_notification:
                await self.notifier.send_notification(
                    title=f"Update Failed: {container.name}",
                    message=f"Failed to update compose-managed container {container.name}: {str(e)}",
                    update_info={
                        "Container": container.name,
                        "Error": str(e),
                        "Project": compose_project,
                        "Service": compose_service
                    },
                    notification_type="error"
                )
            
            return False
    
    async def rollback_container(self, container_name: str, version_id: int):
        """Rollback a container to a previous version"""
        try:
            # Get the version info
            versions = self.db.get_image_versions(container_name)
            version = next((v for v in versions if v['id'] == version_id), None)
            
            if not version:
                logger.error(f"Version {version_id} not found for {container_name}")
                return False
            
            # Get current container and capture current version info BEFORE stopping
            current_image = None
            current_version_display = None
            is_compose_managed = False
            compose_project = None
            compose_service = None
            compose_dir = None
            
            try:
                current_container = self.client.containers.get(container_name)
                current_image = current_container.image
                
                # Check if this is a compose-managed container
                is_compose_managed = 'com.docker.compose.project' in current_container.labels
                if is_compose_managed:
                    compose_project = current_container.labels.get('com.docker.compose.project')
                    compose_service = current_container.labels.get('com.docker.compose.service')
                    compose_dir = current_container.labels.get('com.docker.compose.project.working_dir', '/path/to/compose/dir')
                    logger.info(f"Rolling back compose-managed container {container_name} (project: {compose_project}, service: {compose_service})")
                
                # Get current version from labels
                if current_image.labels:
                    current_version_display = (
                        current_image.labels.get('io.hass.version') or
                        current_image.labels.get('org.opencontainers.image.version') or
                        current_image.labels.get('version') or
                        current_image.labels.get('VERSION')
                    )
                
                if not current_version_display:
                    # Fallback to tag
                    current_version_display = current_image.tags[0] if current_image.tags else current_image.id[:12]
                
                current_container.stop(timeout=30)
                current_container.remove()
            except docker.errors.NotFound:
                pass
            
            # Get the old image (the version we're rolling back TO)
            try:
                old_image = self.client.images.get(version['image_id'])
            except docker.errors.ImageNotFound:
                # Image was pruned/deleted, try to pull it by tag
                logger.info(f"Image {version['image_id'][:12]} not found locally, attempting to pull {version['image_name']}")
                try:
                    old_image = self.client.images.pull(version['image_name'])
                except Exception as pull_error:
                    raise Exception(f"Cannot rollback: Image {version['image_id'][:12]} not found locally and pull failed: {pull_error}")
            
            config = version['container_config']
            
            # Find the best tag to use (prefer versioned tags over 'latest' or 'stable')
            best_tag = version['image_name']  # Default to saved name
            rollback_to_version = None  # For display in logs
            
            # First, try to get version from image labels
            if old_image.labels:
                version_label = (
                    old_image.labels.get('io.hass.version') or  # Home Assistant
                    old_image.labels.get('org.opencontainers.image.version') or  # OCI standard
                    old_image.labels.get('version') or  # Generic
                    old_image.labels.get('VERSION')
                )
                
                if version_label:
                    rollback_to_version = version_label
                    # Construct tag with version from label
                    image_base = best_tag.rsplit(':', 1)[0]  # Remove existing tag
                    best_tag = f"{image_base}:{version_label}"
            
            # If no version in labels, try to find versioned tags
            if not rollback_to_version:
                if old_image.tags:
                    versioned_tags = [tag for tag in old_image.tags if not any(x in tag.lower() for x in [':latest', ':stable', ':dev'])]
                if versioned_tags:
                    best_tag = versioned_tags[0]
                elif old_image.tags:
                    best_tag = old_image.tags[0]
            
            # Store the best tag for the response
            version['best_tag'] = best_tag
            
            # Use binds directly from the saved configuration
            binds = config.get('volumes', [])
            
            # Recreate container with old version
            new_container = self.client.containers.run(
                image=old_image.id,
                name=config['name'],
                environment=config.get('environment'),
                volumes=binds,  # Pass binds directly as list
                ports=config.get('ports'),
                network_mode=config.get('network_mode'),
                restart_policy=config.get('restart_policy'),
                labels=config.get('labels'),
                command=config.get('command'),
                entrypoint=config.get('entrypoint'),
                working_dir=config.get('working_dir'),
                user=config.get('user'),
                hostname=config.get('hostname'),
                extra_hosts=config.get('extra_hosts'),
                privileged=config.get('privileged'),
                cap_add=config.get('cap_add'),
                cap_drop=config.get('cap_drop'),
                devices=config.get('devices'),
                detach=True
            )
            
            # Reconnect to all networks with aliases
            self.reconnect_networks(new_container, config)
            
            logger.info(f"Successfully rolled back {container_name} to version {version_id}")
            
            # Create display message showing version change
            if not rollback_to_version:
                rollback_to_version = version['image_tag']
            
            rollback_message = f"Rolled back from {current_version_display or 'current version'} to {rollback_to_version}"
            
            # Add compose warning if applicable
            if is_compose_managed:
                rollback_message += " (now running as standalone container)"
            
            # Log the rollback to history with version info
            self.db.add_update_history(
                container_name=container_name,
                container_id=new_container.id,
                old_image=current_version_display or (current_image.tags[0] if current_image and current_image.tags else "unknown"),
                new_image=rollback_to_version,
                old_image_id=current_image.id if current_image else "",
                new_image_id=old_image.id,
                status="rollback",
                message=rollback_message
            )
            
            # Send notification if enabled
            if self.config.notifications.email.notify_on_rollback:
                notification_info = {
                    "Container": container_name,
                    "Image": best_tag,
                    "Image Tag": version['image_tag'],
                    "Saved On": version['created_at']
                }
                
                notification_message = f"Successfully rolled back {container_name} to previous version"
                
                # Add compose-specific info and instructions
                if is_compose_managed:
                    notification_message += (
                        f"\n\n⚠️ This was a compose-managed container. "
                        f"It's now running as a STANDALONE container.\n\n"
                        f"To restore compose management:\n"
                        f"1. Stop and remove the standalone container:\n"
                        f"   docker stop {container_name}\n"
                        f"   docker rm {container_name}\n\n"
                        f"2. Go to your compose directory and start the service:\n"
                        f"   cd {compose_dir}\n"
                        f"   docker compose up -d {compose_service}"
                    )
                    notification_info["Original Project"] = compose_project
                    notification_info["Original Service"] = compose_service
                    notification_info["Compose Directory"] = compose_dir
                
                await self.notifier.send_notification(
                    title=f"Container Rolled Back: {container_name}",
                    message=notification_message,
                    update_info=notification_info,
                    notification_type="success"
                )
            
            # Return success with compose info if applicable
            result = {"success": True, "best_tag": best_tag}
            if is_compose_managed:
                result["is_compose"] = True
                result["compose_instructions"] = {
                    "project": compose_project,
                    "service": compose_service,
                    "directory": compose_dir
                }
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to rollback {container_name}: {e}")
            
            # Try to get version info for better logging
            try:
                if 'version' not in locals():
                    versions = self.db.get_image_versions(container_name)
                    version = next((v for v in versions if v['id'] == version_id), None)
            except Exception:
                version = None
            
            # Log failed rollback attempt with as much info as possible
            self.db.add_update_history(
                container_name=container_name,
                container_id="",
                old_image="N/A",
                new_image=version['image_tag'] if version else "N/A",
                old_image_id="",
                new_image_id=version.get('image_id', '') if version else '',
                status="failed",
                message=f"Rollback failed: {str(e)}"
            )
            
            return {"success": False, "error": str(e)}
    
    async def check_all_containers(self):
        """Check all monitored containers for updates"""
        containers = self.get_monitored_containers()
        
        # Log start of batch check
        self.db.add_check_log(
            container_name="batch_check",
            container_id="system",
            current_image="",
            current_image_id="",
            message=f"Started checking {len(containers)} containers for updates"
        )
        logger.info(f"Started checking {len(containers)} containers for updates")
        
        # Track results for summary notification
        results = {
            'checked': 0,
            'updates_found': 0,
            'updates_success': [],
            'updates_failed': [],
            'no_updates': []
        }
        
        for container in containers:
            results['checked'] += 1
            update_info = self.check_for_updates(container)
            
            if update_info:
                results['updates_found'] += 1
                logger.info(f"Update available for {container.name}, starting update...")
                success = await self.update_container(update_info, send_notification=False)
                
                if success:
                    results['updates_success'].append({
                        'name': container.name,
                        'old_image': self._get_image_version(update_info['old_image']),
                        'new_image': self._get_image_version(update_info['new_image'])
                    })
                else:
                    results['updates_failed'].append(container.name)
            else:
                results['no_updates'].append(container.name)
        
        # Log end of batch check
        updated_list = ', '.join([item['name'] for item in results['updates_success']]) if results['updates_success'] else 'none'
        self.db.add_check_log(
            container_name="batch_check",
            container_id="system",
            current_image="",
            current_image_id="",
            message=f"Finished checking {len(containers)} containers for updates, updated {len(results['updates_success'])} containers ({updated_list})"
        )
        logger.info(f"Finished checking {len(containers)} containers for updates, updated {len(results['updates_success'])} containers ({updated_list})")
        
        # Send summary notification
        await self.send_summary_notification(results)
    
    async def send_summary_notification(self, results: Dict):
        """Send a summary notification for all update checks"""
        # Check if batch notifications are enabled
        if not self.config.notifications.email.notify_on_batch_complete:
            return
        
        total_updates = len(results['updates_success']) + len(results['updates_failed'])
        
        if total_updates == 0 and len(results['no_updates']) == 0:
            # Nothing to report
            return
        
        # Build summary message
        title = "Docker Update Summary"
        message = f"Checked {results['checked']} containers\n\n"
        
        update_info = {}
        
        if results['updates_success']:
            message += f"✅ Successfully Updated ({len(results['updates_success'])})\n\n"
            for item in results['updates_success']:
                message += f"  • {item['name']}: {item['old_image']} → {item['new_image']}\n\n"
            update_info['Successfully Updated'] = ', '.join([item['name'] for item in results['updates_success']])
        
        if results['updates_failed']:
            message += f"❌ Failed Updates ({len(results['updates_failed'])})\n\n"
            for name in results['updates_failed']:
                message += f"  • {name}\n\n"
            update_info['Failed Updates'] = ', '.join(results['updates_failed'])
        
        if results['no_updates']:
            message += f"ℹ️ No Updates Available ({len(results['no_updates'])})\n\n"
            for name in results['no_updates']:
                message += f"  • {name}\n\n"
            update_info['No Updates'] = str(len(results['no_updates']))
        
        # Determine notification type
        if results['updates_failed']:
            notification_type = "error"
        elif results['updates_success']:
            notification_type = "success"
        else:
            notification_type = "no_updates"
        
        await self.notifier.send_notification(
            title=title,
            message=message,
            update_info=update_info,
            notification_type=notification_type
        )
    
    async def check_single_container(self, container_name: str):
        """Check a specific container for updates"""
        logger.info(f"Checking container {container_name} for updates")
        
        try:
            container = self.client.containers.get(container_name)
            
            # Check if container should be monitored
            if container.name in self.config.monitoring.exclude_containers:
                logger.warning(f"Container {container_name} is in exclude list")
                return
            
            update_info = self.check_for_updates(container)
            
            if update_info:
                logger.info(f"Update available for {container_name}, starting update...")
                await self.update_container(update_info)
            else:
                logger.info(f"No updates available for {container_name}")
        
        except docker.errors.NotFound:
            logger.error(f"Container {container_name} not found")
        except Exception as e:
            logger.error(f"Error checking container {container_name}: {e}")
    
    def check_container_for_update(self, container_name: str, send_notifications: bool = True) -> Optional[Dict]:
        """Check if a specific container has updates available (without updating)"""
        try:
            container = self.client.containers.get(container_name)
            
            # Check if container should be monitored (skip this check for whalekeeper self-check)
            if container.name != 'whalekeeper' and container.name in self.config.monitoring.exclude_containers:
                logger.warning(f"Container {container_name} is in exclude list")
                return None
            
            update_info = self.check_for_updates(container)
            
            return update_info
        
        except docker.errors.NotFound:
            logger.error(f"Container {container_name} not found")
            return None
        except Exception as e:
            logger.error(f"Error checking container {container_name}: {e}")
            return None
    
    def get_container_image(self, container_name: str) -> str:
        """Get the current image of a container"""
        try:
            container = self.client.containers.get(container_name)
            image_name = container.attrs['Config']['Image']
            return image_name
        except Exception as e:
            logger.error(f"Error getting container image: {e}")
            return "Unknown"
    
    async def update_single_container(self, container_name: str) -> bool:
        """Update a specific container to the latest version"""
        try:
            container = self.client.containers.get(container_name)
            
            # Check if container should be monitored
            if container.name in self.config.monitoring.exclude_containers:
                logger.warning(f"Container {container_name} is in exclude list")
                return False
            
            update_info = self.check_for_updates(container)
            
            if update_info:
                return await self.update_container(update_info)
            else:
                logger.info(f"No updates available for {container_name}")
                return False
        
        except docker.errors.NotFound:
            logger.error(f"Container {container_name} not found")
            return False
        except Exception as e:
            logger.error(f"Error updating container {container_name}: {e}")
            return False


    
    async def start_monitoring(self):
        """Start the monitoring loop"""
        self.running = True
        
        # Start self-check loop in background
        asyncio.create_task(self.self_check_loop())
        
        # Only start cron monitoring if schedule is configured
        if not self.config.cron_schedule or self.config.cron_schedule.strip() == '':
            logger.info("No cron schedule configured - monitoring loop disabled")
            return
        
        logger.info(f"Starting monitoring loop (cron: {self.config.cron_schedule})")
        
        # Create cron iterator
        cron = croniter(self.config.cron_schedule, datetime.now())
        
        while self.running:
            try:
                # Get next scheduled time
                next_run = cron.get_next(datetime)
                wait_seconds = (next_run - datetime.now()).total_seconds()
                
                if wait_seconds > 0:
                    logger.info(f"Next check scheduled at {next_run.strftime('%Y-%m-%d %H:%M:%S')} (in {wait_seconds:.0f}s)")
                    await asyncio.sleep(wait_seconds)
                
                # Run the check
                await self.check_all_containers()
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                # Sleep for a bit before retrying
                await asyncio.sleep(60)
    
    def stop_monitoring(self):
        """Stop the monitoring loop"""
        self.running = False
        logger.info("Stopping monitoring loop")
    
    async def self_check_loop(self):
        """Periodically check if whalekeeper itself has updates (without auto-updating)"""
        logger.info("Starting whalekeeper self-check loop (daily)")
        
        # Check immediately on startup
        await asyncio.sleep(10)  # Wait 10s for app to fully start
        
        while self.running:
            try:
                # Check if whalekeeper has updates (don't send notifications, don't update)
                update_info = self.check_container_for_update('whalekeeper', send_notifications=False)
                
                # Store in cache for UI to display
                if update_info:
                    self.update_cache['whalekeeper'] = update_info
                    logger.info("Whalekeeper self-check: Update available")
                elif 'whalekeeper' in self.update_cache:
                    # Clear from cache if no longer has update
                    del self.update_cache['whalekeeper']
                    logger.info("Whalekeeper self-check: Up to date")
                else:
                    logger.info("Whalekeeper self-check complete - already up to date")
            except Exception as e:
                logger.error(f"Error in whalekeeper self-check: {e}")
            
            # Wait 24 hours before next check
            await asyncio.sleep(86400)
