import docker
import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime
from croniter import croniter

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
    
    def get_monitored_containers(self) -> List[docker.models.containers.Container]:
        """Get list of containers to monitor based on configuration"""
        all_containers = self.client.containers.list()
        
        # Filter out excluded containers
        containers = [
            c for c in all_containers 
            if c.name not in self.config.monitoring.exclude_containers
        ]
        
        # If not monitoring all, filter by labels
        if not self.config.monitoring.monitor_all and self.config.monitoring.labels:
            filtered = []
            for container in containers:
                container_labels = container.labels
                if any(label in container_labels for label in self.config.monitoring.labels):
                    filtered.append(container)
            containers = filtered
        
        return containers
    
    def check_for_updates(self, container) -> Optional[Dict]:
        """Check if a newer image is available for a container"""
        try:
            # Get current image
            current_image = container.image
            image_name = container.attrs['Config']['Image']
            
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
        
        return {
            'image': attrs['Config']['Image'],
            'name': container.name,
            'environment': config.get('Env', []),
            'volumes': host_config.get('Binds', []),
            'ports': host_config.get('PortBindings', {}),
            'network_mode': host_config.get('NetworkMode', 'bridge'),
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
    
    async def update_container(self, update_info: Dict, send_notification: bool = True) -> bool:
        """Update a container with the new image"""
        container = update_info['container']
        old_image = update_info['old_image']
        new_image = update_info['new_image']
        image_name = update_info['image_name']
        
        try:
            # Save current configuration for rollback
            container_config = self.get_container_config(container)
            
            # Extract tag from image name
            image_tag = image_name.split(':')[-1] if ':' in image_name else 'latest'
            
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
            
            # Record success
            self.db.add_update_history(
                container_name=container.name,
                container_id=new_container.id,
                old_image=old_image.tags[0] if old_image.tags else old_image.id[:12],
                new_image=new_image.tags[0] if new_image.tags else new_image.id[:12],
                old_image_id=old_image.id,
                new_image_id=new_image.id,
                status="success",
                message="Container updated successfully"
            )
            
            # Send notification (only for individual updates, not batch)
            if send_notification:
                await self.notifier.send_notification(
                    title=f"Container Updated: {container.name}",
                    message=f"Successfully updated container {container.name}",
                    update_info={
                        "Container": container.name,
                        "Old Image": old_image.tags[0] if old_image.tags else old_image.id[:12],
                        "New Image": new_image.tags[0] if new_image.tags else new_image.id[:12],
                        "Status": "Success"
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
                old_image=old_image.tags[0] if old_image.tags else old_image.id[:12],
                new_image=new_image.tags[0] if new_image.tags else new_image.id[:12],
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
    
    async def rollback_container(self, container_name: str, version_id: int) -> bool:
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
            try:
                current_container = self.client.containers.get(container_name)
                current_image = current_container.image
                
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
            old_image = self.client.images.get(version['image_id'])
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
            
            logger.info(f"Successfully rolled back {container_name} to version {version_id}")
            
            # Create display message showing version change
            if not rollback_to_version:
                rollback_to_version = version['image_tag']
            
            rollback_message = f"Rolled back from {current_version_display or 'current version'} to {rollback_to_version}"
            
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
            
            # Send notification
            await self.notifier.send_notification(
                title=f"Container Rolled Back: {container_name}",
                message=f"Successfully rolled back {container_name} to previous version",
                update_info={
                    "Container": container_name,
                    "Image": best_tag,
                    "Image Tag": version['image_tag'],
                    "Saved On": version['created_at']
                },
                notification_type="success"
            )
            
            return {"success": True, "best_tag": best_tag}
            
        except Exception as e:
            logger.error(f"Failed to rollback {container_name}: {e}")
            
            # Log failed rollback attempt
            self.db.add_update_history(
                container_name=container_name,
                container_id="",
                old_image="",
                new_image=version.get('image_name', '') if 'version' in locals() else '',
                old_image_id="",
                new_image_id="",
                status="failed",
                message=f"Rollback failed: {str(e)}"
            )
            
            return {"success": False}
    
    async def check_all_containers(self):
        """Check all monitored containers for updates"""
        logger.info("Starting container update check")
        
        containers = self.get_monitored_containers()
        logger.info(f"Monitoring {len(containers)} containers")
        
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
                        'old_image': update_info['old_image'].tags[0] if update_info['old_image'].tags else update_info['old_image'].id[:12],
                        'new_image': update_info['new_image'].tags[0] if update_info['new_image'].tags else update_info['new_image'].id[:12]
                    })
                else:
                    results['updates_failed'].append(container.name)
            else:
                results['no_updates'].append(container.name)
        
        logger.info("Container update check complete")
        
        # Send summary notification
        await self.send_summary_notification(results)
    
    async def send_summary_notification(self, results: Dict):
        """Send a summary notification for all update checks"""
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
    
    def check_container_for_update(self, container_name: str) -> Optional[Dict]:
        """Check if a specific container has updates available (without updating)"""
        try:
            container = self.client.containers.get(container_name)
            
            # Check if container should be monitored
            if container.name in self.config.monitoring.exclude_containers:
                logger.warning(f"Container {container_name} is in exclude list")
                return None
            
            update_info = self.check_for_updates(container)
            
            # Log the check if no updates found
            if not update_info:
                current_image = container.image
                self.db.add_check_log(
                    container_name=container.name,
                    container_id=container.id,
                    current_image=current_image.tags[0] if current_image.tags else current_image.id[:12],
                    current_image_id=current_image.id,
                    message="Checked for updates - already up to date"
                )
                
                # Send notification if configured
                import asyncio
                asyncio.create_task(self.notifier.send_notification(
                    title=f"No Updates Available: {container.name}",
                    message=f"Container {container.name} is already running the latest version",
                    update_info={
                        "Container": container.name,
                        "Current Image": current_image.tags[0] if current_image.tags else current_image.id[:12]
                    },
                    notification_type="no_updates"
                ))
            else:
                # Send notification about update found
                import asyncio
                asyncio.create_task(self.notifier.send_notification(
                    title=f"Update Available: {container.name}",
                    message=f"A new version is available for {container.name}",
                    update_info={
                        "Container": container.name,
                        "Current Image": update_info['old_image'].tags[0] if update_info['old_image'].tags else update_info['old_image'].id[:12],
                        "New Image": update_info['new_image'].tags[0] if update_info['new_image'].tags else update_info['new_image'].id[:12]
                    },
                    notification_type="update_found"
                ))
            
            return update_info
        
        except docker.errors.NotFound:
            logger.error(f"Container {container_name} not found")
            return None
        except Exception as e:
            logger.error(f"Error checking container {container_name}: {e}")
            
            # Send error notification
            import asyncio
            asyncio.create_task(self.notifier.send_notification(
                title=f"Error Checking Updates: {container_name}",
                message=f"Failed to check for updates: {str(e)}",
                update_info={
                    "Container": container_name,
                    "Error": str(e)
                },
                notification_type="error"
            ))
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
