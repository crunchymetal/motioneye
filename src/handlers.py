
# Copyright (c) 2013 Calin Crisan
# This file is part of motionEye.
#
# motionEye is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>. 

import datetime
import json
import logging
import os
import re
import socket

from tornado.web import RequestHandler, HTTPError, asynchronous
from tornado.ioloop import IOLoop

import config
import mediafiles
import motionctl
import powerctl
import remote
import settings
import smbctl
import template
import update
import utils
import v4l2ctl


class BaseHandler(RequestHandler):
    def get_data(self):
        keys = self.request.arguments.keys()
        data = dict([(key, self.get_argument(key)) for key in keys])

        for key in self.request.files:
            files = self.request.files[key]
            if len(files) > 1:
                data[key] = files

            elif len(files) > 0:
                data[key] = files[0]

            else:
                continue

        return data
    
    def render(self, template_name, content_type='text/html', **context):
        self.set_header('Content-Type', content_type)
        
        content = template.render(template_name, **context)
        self.finish(content)
    
    def finish_json(self, data={}):
        self.set_header('Content-Type', 'application/json')
        self.finish(json.dumps(data))

    def get_current_user(self):
        main_config = config.get_main()
        
        username = self.get_argument('_username', None)
        signature = self.get_argument('_signature', None)
        login = self.get_argument('_login', None) == 'true'
        if (username == main_config.get('@admin_username') and
            signature == utils.compute_signature(self.request.method, self.request.uri, self.request.body, main_config.get('@admin_password'))):
            
            return 'admin'
        
        elif not username and not main_config.get('@normal_password'): # no authentication required for normal user
            return 'normal'
        
        elif (username == main_config.get('@normal_username') and
            signature == utils.compute_signature(self.request.method, self.request.uri, self.request.body, main_config.get('@normal_password'))):
            
            return 'normal'

        elif username and username != '_' and login:
            logging.error('authentication failed for user %(user)s' % {'user': username})

        return None
        
    def _handle_request_exception(self, exception):
        try:
            if isinstance(exception, HTTPError):
                logging.error(str(exception))
                self.set_status(exception.status_code)
                self.finish_json({'error': exception.log_message or getattr(exception, 'reason', None) or str(exception)})
            
            else:
                logging.error(str(exception), exc_info=True)
                self.set_status(500)
                self.finish_json({'error':  'internal server error'})
                
        except RuntimeError:
            pass # nevermind
        
    @staticmethod
    def auth(admin=False, prompt=True):
        def decorator(func):
            def wrapper(self, *args, **kwargs):
                _admin = self.get_argument('_admin', None) == 'true'
                
                user = self.current_user
                if (user is None) or (user != 'admin' and (admin or _admin)):
                    self.set_header('Content-Type', 'application/json')

                    return self.finish_json({'error': 'unauthorized', 'prompt': prompt})

                return func(self, *args, **kwargs)
            
            return wrapper
        
        return decorator

    def get(self, *args, **kwargs):
        raise HTTPError(400, 'method not allowed')

    def post(self, *args, **kwargs):
        raise HTTPError(400, 'method not allowed')


class NotFoundHandler(BaseHandler):
    def get(self):
        raise HTTPError(404, 'not found')

    def post(self):
        raise HTTPError(404, 'not found')


class MainHandler(BaseHandler):
    def get(self):
        import motioneye
        
        # additional config
        main_sections = config.get_additional_structure(camera=False, separators=True)[0]
        camera_sections = config.get_additional_structure(camera=True, separators=True)[0]

        self.render('main.html',
                frame=False,
                version=motioneye.VERSION,
                enable_update=False,
                enable_reboot=settings.ENABLE_REBOOT,
                main_sections=main_sections,
                camera_sections=camera_sections,
                hostname=socket.gethostname(),
                title=self.get_argument('title', None),
                admin_username=config.get_main().get('@admin_username'),
                old_motion=config.is_old_motion())


class ConfigHandler(BaseHandler):
    @asynchronous
    def get(self, camera_id=None, op=None):
        if camera_id is not None:
            camera_id = int(camera_id)
        
        if op == 'get':
            self.get_config(camera_id)
            
        elif op == 'list':
            self.list_cameras()
        
        elif op == 'list_devices':
            self.list_devices()
            
        elif op == 'backup':
            self.backup()
        
        else:
            raise HTTPError(400, 'unknown operation')
    
    @asynchronous
    def post(self, camera_id=None, op=None):
        if camera_id is not None:
            camera_id = int(camera_id)
        
        if op == 'set':
            self.set_config(camera_id)
        
        elif op == 'set_preview':
            self.set_preview(camera_id)
        
        elif op == 'add':
            self.add_camera()
        
        elif op == 'rem':
            self.rem_camera(camera_id)
            
        elif op == 'restore':
            self.restore()
        
        elif op == '_relay_event':
            self._relay_event(camera_id)
        
        else:
            raise HTTPError(400, 'unknown operation')
    
    @BaseHandler.auth(admin=True)
    def get_config(self, camera_id):
        if camera_id:
            logging.debug('getting config for camera %(id)s' % {'id': camera_id})
            
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')
            
            local_config = config.get_camera(camera_id)
            if utils.local_motion_camera(local_config):
                ui_config = config.camera_dict_to_ui(local_config)
                    
                self.finish_json(ui_config)
            
            elif utils.remote_camera(local_config):
                def on_response(remote_ui_config=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to get remote camera configuration for %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(local_config), 'msg': error}})
                    
                    for key, value in local_config.items():
                        remote_ui_config[key.replace('@', '')] = value
                    
                    # replace the real device URI with the remote camera URL
                    remote_ui_config['device_url'] = remote.pretty_camera_url(local_config)
                    self.finish_json(remote_ui_config)
                
                remote.get_config(local_config, on_response)
                
            else: # assuming simple mjpeg camera
                pass # TODO implement me
            
        else:
            logging.debug('getting main config')
            
            ui_config = config.main_dict_to_ui(config.get_main())
            self.finish_json(ui_config)
    
    @BaseHandler.auth(admin=True)
    def set_config(self, camera_id):
        try:
            ui_config = json.loads(self.request.body)
            
        except Exception as e:
            logging.error('could not decode json: %(msg)s' % {'msg': unicode(e)})
            
            raise
        
        camera_ids = config.get_camera_ids()
        
        def set_camera_config(camera_id, ui_config, on_finish):
            logging.debug('setting config for camera %(id)s...' % {'id': camera_id})
            
            if camera_id not in camera_ids:
                raise HTTPError(404, 'no such camera')
            
            local_config = config.get_camera(camera_id)
            if utils.local_motion_camera(local_config):
                local_config = config.camera_ui_to_dict(ui_config, local_config)

                config.set_camera(camera_id, local_config)
            
                on_finish(None, True) # (no error, motion needs restart)

            elif utils.remote_camera(local_config):
                # update the camera locally
                local_config['@enabled'] = ui_config['enabled']
                config.set_camera(camera_id, local_config)
                
                if ui_config.has_key('name'):
                    def on_finish_wrapper(error=None):
                        return on_finish(error, False)
                    
                    ui_config['enabled'] = True # never disable the camera remotely 
                    remote.set_config(local_config, ui_config, on_finish_wrapper)
                
                else:
                    # when the ui config supplied has only the enabled state
                    # and no useful fields (such as "name"),
                    # the camera was probably disabled due to errors
                    on_finish(None, False)
                    
            else: # assuming simple mjpeg camera
                pass # TODO implement me

        def set_main_config(ui_config):
            logging.debug('setting main config...')
            
            old_main_config = config.get_main()
            old_admin_credentials = '%s:%s' % (old_main_config.get('@admin_username', ''), old_main_config.get('@admin_password', ''))
            old_normal_credentials = '%s:%s' % (old_main_config.get('@normal_username', ''), old_main_config.get('@normal_password', ''))

            main_config = config.main_ui_to_dict(ui_config)
            main_config.setdefault('thread', old_main_config.get('thread', [])) 
            admin_credentials = '%s:%s' % (main_config.get('@admin_username', ''), main_config.get('@admin_password', ''))
            normal_credentials = '%s:%s' % (main_config.get('@normal_username', ''), main_config.get('@normal_password', ''))

            additional_configs = config.get_additional_structure(camera=False)[1]           
            reboot_config_names = [('@_' + c['name']) for c in additional_configs.values() if c.get('reboot')]
            reboot_config_names.append('@admin_password')
            reboot = bool([k for k in reboot_config_names if old_main_config.get(k) != main_config.get(k)])

            config.set_main(main_config)
            
            reload = False
            restart = False
            
            if admin_credentials != old_admin_credentials:
                logging.debug('admin credentials changed, reload needed')
                
                reload = True

            if normal_credentials != old_normal_credentials:
                logging.debug('surveillance credentials changed, all camera configs must be updated')
                
                # reconfigure all local cameras to update the stream authentication options
                for camera_id in config.get_camera_ids():
                    local_config = config.get_camera(camera_id)
                    if not utils.local_motion_camera(local_config):
                        continue
                    
                    ui_config = config.camera_dict_to_ui(local_config)
                    local_config = config.camera_ui_to_dict(ui_config, local_config)

                    config.set_camera(camera_id, local_config)
                    
                    restart = True

            if reboot and settings.ENABLE_REBOOT:
                logging.debug('system settings changed, reboot needed')
        
            else: 
                reboot = False

            return {'reload': reload, 'reboot': reboot, 'restart': restart}
        
        reload = False # indicates that browser should reload the page
        reboot = [False] # indicates that the server will reboot immediately
        restart = [False]  # indicates that the local motion instance was modified and needs to be restarted
        error = [None]
        
        def finish():
            if reboot[0]:
                if settings.ENABLE_REBOOT:
                    def call_reboot():
                        powerctl.reboot()
                    
                    ioloop = IOLoop.instance()
                    ioloop.add_timeout(datetime.timedelta(seconds=2), call_reboot)
                    return self.finish({'reload': False, 'reboot': True, 'error': None})
                
                else:
                    reboot[0] = False

            if restart[0]:
                logging.debug('motion needs to be restarted')
                
                motionctl.stop()
                
                if settings.SMB_SHARES:
                    logging.debug('updating SMB mounts')
                    stop, start = smbctl.update_mounts()  # @UnusedVariable

                    if start:
                        motionctl.start()
                
                else:
                    motionctl.start()

            self.finish({'reload': reload, 'reboot': reboot[0], 'error': error[0]})
        
        if camera_id is not None:
            if camera_id == 0: # multiple camera configs
                if len(ui_config) > 1:
                    logging.debug('setting multiple configs')
                
                elif len(ui_config) == 0:
                    logging.warn('no configuration to set')
                    
                    self.finish()
                
                so_far = [0]
                def check_finished(e, r):
                    restart[0] = restart[0] or r
                    error[0] = error[0] or e
                    so_far[0] += 1
                    
                    if so_far[0] >= len(ui_config): # finished
                        finish()

                # make sure main config is handled first
                items = ui_config.items()
                items.sort(key=lambda (key, cfg): key != 'main')

                for key, cfg in items:
                    if key == 'main':
                        result = set_main_config(cfg)
                        reload = result['reload'] or reload
                        reboot[0] = result['reboot'] or reboot[0]
                        restart[0] = result['restart'] or restart[0]
                        check_finished(None, reload)
                        
                    else:
                        set_camera_config(int(key), cfg, check_finished)
            
            else: # single camera config
                def on_finish(e, r):
                    error[0] = e
                    restart[0] = r
                    finish()

                set_camera_config(camera_id, ui_config, on_finish)

        else: # main config
            result = set_main_config(ui_config)
            reload = result['reload']
            reboot[0] = result['reboot']
            restart[0] = result['restart']

    @BaseHandler.auth(admin=True)
    def set_preview(self, camera_id):
        try:
            controls = json.loads(self.request.body)
            
        except Exception as e:
            logging.error('could not decode json: %(msg)s' % {'msg': unicode(e)})
            
            raise

        camera_config = config.get_camera(camera_id)
        if utils.v4l2_camera(camera_config): 
            device = camera_config['videodevice']
            
            if 'brightness' in controls:
                value = int(controls['brightness'])
                logging.debug('setting brightness to %(value)s...' % {'value': value})
    
                v4l2ctl.set_brightness(device, value)
    
            if 'contrast' in controls:
                value = int(controls['contrast'])
                logging.debug('setting contrast to %(value)s...' % {'value': value})
    
                v4l2ctl.set_contrast(device, value)
    
            if 'saturation' in controls:
                value = int(controls['saturation'])
                logging.debug('setting saturation to %(value)s...' % {'value': value})
    
                v4l2ctl.set_saturation(device, value)
    
            if 'hue' in controls:
                value = int(controls['hue'])
                logging.debug('setting hue to %(value)s...' % {'value': value})
    
                v4l2ctl.set_hue(device, value)
            
            self.finish_json({})

        elif utils.remote_camera(camera_config):
            def on_response(error=None):
                if error:
                    self.finish_json({'error': error})
                    
                else:
                    self.finish_json()
            
            remote.set_preview(camera_config, controls, on_response)
        
        else: # not supported
            self.finish_json({'error': True})

    @BaseHandler.auth()
    def list_cameras(self):
        logging.debug('listing cameras')

        proto = self.get_data().get('proto')        
        if proto == 'motioneye':  # remote listing
            def on_response(cameras=None, error=None):
                if error:
                    self.finish_json({'error': error})
                    
                else:
                    cameras = [c for c in cameras if c.get('enabled')]
                    self.finish_json({'cameras': cameras})

            remote.list_cameras(self.get_data(), on_response)
        
        elif proto == 'netcam':
            def on_response(cameras=None, error=None):
                if error:
                    self.finish_json({'error': error})
                    
                else:
                    self.finish_json({'cameras': cameras})
            
            utils.test_mjpeg_url(self.get_data(), auth_modes=['basic'], allow_jpeg=True, callback=on_response)
                
        elif proto == 'mjpeg':
            def on_response(cameras=None, error=None):
                if error:
                    self.finish_json({'error': error})
                    
                else:
                    self.finish_json({'cameras': cameras})
            
            utils.test_mjpeg_url(self.get_data(), auth_modes=['basic', 'digest'], allow_jpeg=False, callback=on_response)
                
        else:  # assuming local motionEye camera listing
            cameras = []
            camera_ids = config.get_camera_ids()
            if not config.get_main().get('@enabled'):
                camera_ids = []
                
            length = [len(camera_ids)]
            def check_finished():
                if len(cameras) == length[0]:
                    cameras.sort(key=lambda c: c['id'])
                    self.finish_json({'cameras': cameras})
                    
            def on_response_builder(camera_id, local_config):
                def on_response(remote_ui_config=None, error=None):
                    if error:
                        cameras.append({
                            'id': camera_id,
                            'name': '&lt;' + remote.pretty_camera_url(local_config) + '&gt;',
                            'enabled': False,
                            'streaming_framerate': 1,
                            'framerate': 1
                        })
                    
                    else:
                        remote_ui_config['id'] = camera_id

                        if not remote_ui_config['enabled'] and local_config['@enabled']:
                            # if a remote camera is disabled, make sure it's disabled locally as well
                            local_config['@enabled'] = False
                            config.set_camera(camera_id, local_config)
                        
                        elif remote_ui_config['enabled'] and not local_config['@enabled']:
                            # if a remote camera is locally disabled, make sure the remote config says the same thing
                            remote_ui_config['enabled'] = False
                            
                        for key, value in local_config.items():
                            remote_ui_config[key.replace('@', '')] = value

                        cameras.append(remote_ui_config)
                        
                    check_finished()
                    
                return on_response
            
            for camera_id in camera_ids:
                local_config = config.get_camera(camera_id)
                if local_config is None:
                    continue
                
                if utils.local_motion_camera(local_config):
                    ui_config = config.camera_dict_to_ui(local_config)
                    cameras.append(ui_config)
                    check_finished()

                elif utils.remote_camera(local_config):
                    if local_config.get('@enabled') or self.get_argument('force', None) == 'true':
                        remote.get_config(local_config, on_response_builder(camera_id, local_config))
                    
                    else: # don't try to reach the remote of the camera is disabled
                        on_response_builder(camera_id, local_config)(error=True)
                        
                else: # assuming simple mjpeg camera
                    pass # TODO implement me
            
            if length[0] == 0:        
                self.finish_json({'cameras': []})

    @BaseHandler.auth(admin=True)
    def list_devices(self):
        logging.debug('listing devices')
        
        configured_devices = {}
        for camera_id in config.get_camera_ids():
            data = config.get_camera(camera_id)
            if utils.v4l2_camera(data):
                configured_devices[data['videodevice']] = True

        devices = [{'uri': d[0], 'name': d[1], 'configured': d[0] in configured_devices}
                for d in v4l2ctl.list_devices()]
        
        self.finish_json({'devices': devices})
    
    @BaseHandler.auth(admin=True)
    def add_camera(self):
        logging.debug('adding new camera')
        
        try:
            device_details = json.loads(self.request.body)
            
        except Exception as e:
            logging.error('could not decode json: %(msg)s' % {'msg': unicode(e)})
            
            raise

        camera_config = config.add_camera(device_details)

        if utils.local_motion_camera(camera_config):
            motionctl.stop()
            
            if settings.SMB_SHARES:
                stop, start = smbctl.update_mounts()  # @UnusedVariable

                if start:
                    motionctl.start()
            
            else:
                motionctl.start()
            
            ui_config = config.camera_dict_to_ui(camera_config)
            
            self.finish_json(ui_config)
        
        elif utils.remote_camera(camera_config):
            def on_response(remote_ui_config=None, error=None):
                if error:
                    return self.finish_json({'error': error})

                for key, value in camera_config.items():
                    remote_ui_config[key.replace('@', '')] = value
                
                self.finish_json(remote_ui_config)
                
            remote.get_config(camera_config, on_response)
        
        else: # assuming simple mjpeg camera
            #ui_config = config.camera_dict_to_ui(camera_config)
            # TODO use a special mjpeg function to generate ui_config
            self.finish_json(ui_config)
    
    @BaseHandler.auth(admin=True)
    def rem_camera(self, camera_id):
        logging.debug('removing camera %(id)s' % {'id': camera_id})
        
        local = utils.local_motion_camera(config.get_camera(camera_id))
        config.rem_camera(camera_id)
        
        if local:
            motionctl.stop()
            motionctl.start()
            
        self.finish_json()
        
    @BaseHandler.auth(admin=True)
    def backup(self):
        content = config.backup()

        filename = 'motioneye-config.tar.gz'
        self.set_header('Content-Type', 'application/x-compressed')
        self.set_header('Content-Disposition', 'attachment; filename=' + filename + ';')

        self.finish(content)

    @BaseHandler.auth(admin=True)
    def restore(self):
        try:
            content = self.request.files['files'][0]['body']
            
        except KeyError:
            raise HTTPError(400, 'file attachment required')

        result = config.restore(content)
        if result:
            self.finish_json({'ok': True, 'reboot': result['reboot']})
            
        else:
            self.finish_json({'ok': False})

    @BaseHandler.auth(admin=True)
    def _relay_event(self, camera_id):
        event = self.get_argument('event')
        logging.debug('event %(event)s relayed for camera with id %(id)s' % {'event': event, 'id': camera_id})
        
        try:
            camera_config = config.get_camera(camera_id)
        
        except:
            logging.warn('ignoring event for remote camera with id %s (probably removed)' % camera_id)
            return self.finish_json()

        if not utils.local_motion_camera(camera_config):
            logging.warn('ignoring event for non-local camera with id %s' % camera_id)
            return self.finish_json()
        
        if event == 'start':
            if not camera_config['@motion_detection']:
                logging.debug('ignoring start event for camera with id %s and motion detection disabled' % camera_id)
                return self.finish_json()

            motionctl._motion_detected[camera_id] = True
            
        elif event == 'stop':
            motionctl._motion_detected[camera_id] = False
            
        else:
            logging.warn('unknown event %s' % event)

        self.finish_json()


class PictureHandler(BaseHandler):
    @asynchronous
    def get(self, camera_id, op, filename=None, group=None):
        if camera_id is not None:
            camera_id = int(camera_id)
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')
        
        if op == 'current':
            self.current(camera_id)
            
        elif op == 'list':
            self.list(camera_id)
            
        elif op == 'frame':
            self.frame(camera_id)
            
        elif op == 'download':
            self.download(camera_id, filename)
        
        elif op == 'preview':
            self.preview(camera_id, filename)
        
        elif op == 'zipped':
            self.zipped(camera_id, group)
        
        elif op == 'timelapse':
            self.timelapse(camera_id, group)
        
        else:
            raise HTTPError(400, 'unknown operation')
    
    @asynchronous
    def post(self, camera_id, op, filename=None, group=None):
        if camera_id is not None:
            camera_id = int(camera_id)
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')
        
        if op == 'delete':
            self.delete(camera_id, filename)

        elif op == 'delete_all':
            self.delete_all(camera_id, group)
        
        else:
            raise HTTPError(400, 'unknown operation')
    
    @BaseHandler.auth(prompt=False)
    def current(self, camera_id):
        self.set_header('Content-Type', 'image/jpeg')
        
        sequence = self.get_argument('seq', None)
        if sequence:
            sequence = int(sequence)
        
        width = self.get_argument('width', None)
        height = self.get_argument('height', None)
        
        picture = sequence and mediafiles.get_picture_cache(camera_id, sequence, width) or None

        if picture is not None:
            return self.try_finish(picture)

        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            picture = mediafiles.get_current_picture(camera_config,
                    width=width,
                    height=height)
            
            if sequence and picture:
                mediafiles.set_picture_cache(camera_id, sequence, width, picture)

            self.set_cookie('motion_detected_' + str(camera_id), str(motionctl.is_motion_detected(camera_id)).lower())
            self.try_finish(picture)
                
        elif utils.remote_camera(camera_config):
            def on_response(motion_detected=False, picture=None, error=None):
                if sequence and picture:
                    mediafiles.set_picture_cache(camera_id, sequence, width, picture)
                
                self.set_cookie('motion_detected_' + str(camera_id), str(motion_detected).lower())
                self.try_finish(picture)
            
            remote.get_current_picture(camera_config, width=width, height=height, callback=on_response)
            
        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')
            

    @BaseHandler.auth()
    def list(self, camera_id):
        logging.debug('listing pictures for camera %(id)s' % {'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            def on_media_list(media_list):
                if media_list is None:
                    return self.finish_json({'error': 'Failed to get pictures list.'})

                self.finish_json({
                    'mediaList': media_list,
                    'cameraName': camera_config['@name']
                })
            
            mediafiles.list_media(camera_config, media_type='picture',
                    callback=on_media_list, prefix=self.get_argument('prefix', None))

        elif utils.remote_camera(camera_config):
            def on_response(remote_list=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to get picture list for %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json(remote_list)
            
            remote.list_media(camera_config, media_type='picture', prefix=self.get_argument('prefix', None), callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    def frame(self, camera_id):
        camera_config = config.get_camera(camera_id)
        
        if utils.local_motion_camera(camera_config) or self.get_argument('title', None) is not None:
            self.render('main.html',
                    frame=True,
                    camera_id=camera_id,
                    camera_config=camera_config,
                    title=self.get_argument('title', camera_config.get('@name', '')),
                    admin_username=config.get_main().get('@admin_username'))

        elif utils.remote_camera(camera_config):
            def on_response(remote_ui_config=None, error=None):
                if error:
                    return self.render('main.html',
                            frame=True,
                            camera_id=camera_id,
                            camera_config=camera_config,
                            title=self.get_argument('title', ''))

                # issue a fake camera_ui_to_dict() call to transform
                # the remote UI values into motion config directives
                remote_config = config.camera_ui_to_dict(remote_ui_config)
                
                self.render('main.html',
                        frame=True,
                        camera_id=camera_id,
                        camera_config=remote_config,
                        title=self.get_argument('title', remote_config['@name']),
                        admin_username=config.get_main().get('@admin_username'))

            remote.get_config(camera_config, on_response)
        
        else: # assuming simple mjpeg camera
            pass # TODO implement me

    @BaseHandler.auth()
    def download(self, camera_id, filename):
        logging.debug('downloading picture %(filename)s of camera %(id)s' % {
                'filename': filename, 'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            content = mediafiles.get_media_content(camera_config, filename, 'picture')
            
            pretty_filename = camera_config['@name'] + '_' + os.path.basename(filename)
            self.set_header('Content-Type', 'image/jpeg')
            self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')
            
            self.finish(content)
        
        elif utils.remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to download picture from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                pretty_filename = os.path.basename(filename) # no camera name available w/o additional request
                self.set_header('Content-Type', 'image/jpeg')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')
                
                self.finish(response)

            remote.get_media_content(camera_config, filename=filename, media_type='picture', callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def preview(self, camera_id, filename):
        logging.debug('previewing picture %(filename)s of camera %(id)s' % {
                'filename': filename, 'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            content = mediafiles.get_media_preview(camera_config, filename, 'picture',
                    width=self.get_argument('width', None),
                    height=self.get_argument('height', None))
            
            if content:
                self.set_header('Content-Type', 'image/jpeg')
                
            else:
                self.set_header('Content-Type', 'image/svg+xml')
                content = open(os.path.join(settings.STATIC_PATH, 'img', 'no-preview.svg')).read()
                
            self.finish(content)
        
        elif utils.remote_camera(camera_config):
            def on_response(content=None, error=None):
                if content:
                    self.set_header('Content-Type', 'image/jpeg')
                    
                else:
                    self.set_header('Content-Type', 'image/svg+xml')
                    content = open(os.path.join(settings.STATIC_PATH, 'img', 'no-preview.svg')).read()
                
                self.finish(content)
            
            remote.get_media_preview(camera_config, filename=filename, media_type='picture',
                    width=self.get_argument('width', None),
                    height=self.get_argument('height', None),
                    callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')
    
    @BaseHandler.auth(admin=True)
    def delete(self, camera_id, filename):
        logging.debug('deleting picture %(filename)s of camera %(id)s' % {
                'filename': filename, 'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            try:
                mediafiles.del_media_content(camera_config, filename, 'picture')
                self.finish_json()
                
            except Exception as e:
                self.finish_json({'error': unicode(e)})

        elif utils.remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to delete picture from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json()

            remote.del_media_content(camera_config, filename=filename, media_type='picture', callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def zipped(self, camera_id, group):
        key = self.get_argument('key', None)
        camera_config = config.get_camera(camera_id)
        
        if key:
            logging.debug('serving zip file for group %(group)s of camera %(id)s with key %(key)s' % {
                    'group': group, 'id': camera_id, 'key': key})
            
            if utils.local_motion_camera(camera_config):
                data = mediafiles.get_prepared_cache(key)
                if not data:
                    logging.error('prepared cache data for key "%s" does not exist' % key)
                    
                    raise HTTPError(404, 'no such key')

                pretty_filename = camera_config['@name'] + '_' + group
                pretty_filename = re.sub('[^a-zA-Z0-9]', '_', pretty_filename)
         
                self.set_header('Content-Type', 'application/zip')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + '.zip;')
                self.finish(data)
                
            elif utils.remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to download zip file from %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    self.set_header('Content-Type', response['content_type'])
                    self.set_header('Content-Disposition', response['content_disposition'])
                    self.finish(response['data'])

                remote.get_zipped_content(camera_config, media_type='picture', key=key, group=group, callback=on_response)

            else: # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

        else: # prepare
            logging.debug('preparing zip file for group %(group)s of camera %(id)s' % {
                    'group': group, 'id': camera_id})

            if utils.local_motion_camera(camera_config):
                def on_zip(data):
                    if data is None:
                        return self.finish_json({'error': 'Failed to create zip file.'})
    
                    key = mediafiles.set_prepared_cache(data)
                    logging.debug('prepared zip file for group %(group)s of camera %(id)s with key %(key)s' % {
                            'group': group, 'id': camera_id, 'key': key})
                    self.finish_json({'key': key})
    
                mediafiles.get_zipped_content(camera_config, media_type='picture', group=group, callback=on_zip)
    
            elif utils.remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to make zip file at %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    self.finish_json({'key': response['key']})

                remote.make_zipped_content(camera_config, media_type='picture', group=group, callback=on_response)

            else: # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def timelapse(self, camera_id, group):
        key = self.get_argument('key', None)
        check = self.get_argument('check', False)
        camera_config = config.get_camera(camera_id)

        if key: # download
            logging.debug('serving timelapse movie for group %(group)s of camera %(id)s with key %(key)s' % {
                    'group': group, 'id': camera_id, 'key': key})
            
            if utils.local_motion_camera(camera_config):
                data = mediafiles.get_prepared_cache(key)
                if data is None:
                    logging.error('prepared cache data for key "%s" does not exist' % key)

                    raise HTTPError(404, 'no such key')

                pretty_filename = camera_config['@name'] + '_' + group
                pretty_filename = re.sub('[^a-zA-Z0-9]', '_', pretty_filename)
    
                self.set_header('Content-Type', 'video/x-msvideo')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + '.avi;')
                self.finish(data)

            elif utils.remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to download timelapse movie from %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    self.set_header('Content-Type', response['content_type'])
                    self.set_header('Content-Disposition', response['content_disposition'])
                    self.finish(response['data'])

                remote.get_timelapse_movie(camera_config, key, group=group, callback=on_response)

            else: # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

        elif check:
            logging.debug('checking timelapse movie status for group %(group)s of camera %(id)s' % {
                    'group': group, 'id': camera_id})

            if utils.local_motion_camera(camera_config):
                status = mediafiles.check_timelapse_movie()
                if status['progress'] == -1 and status['data']:
                    key = mediafiles.set_prepared_cache(status['data'])
                    logging.debug('prepared timelapse movie for group %(group)s of camera %(id)s with key %(key)s' % {
                            'group': group, 'id': camera_id, 'key': key})
                    self.finish_json({'key': key, 'progress': -1})

                else:
                    self.finish_json(status)

            elif utils.remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to check timelapse movie progress at %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    if response['progress'] == -1 and response.get('key'):
                        self.finish_json({'key': response['key'], 'progress': -1})
                    
                    else:
                        self.finish_json(response)

                remote.check_timelapse_movie(camera_config, group=group, callback=on_response)

            else: # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

        else: # start timelapse
            interval = int(self.get_argument('interval'))
            framerate = int(self.get_argument('framerate'))

            logging.debug('preparing timelapse movie for group %(group)s of camera %(id)s with rate %(framerate)s/%(int)s' % {
                    'group': group, 'id': camera_id, 'framerate': framerate, 'int': interval})

            if utils.local_motion_camera(camera_config):
                status = mediafiles.check_timelapse_movie()
                if status['progress'] != -1:
                    self.finish_json({'progress': status['progress']}) # timelapse already active

                else:
                    mediafiles.make_timelapse_movie(camera_config, framerate, interval, group=group)
                    self.finish_json({'progress': -1})

            elif utils.remote_camera(camera_config):
                def on_status(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to make timelapse movie at %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(camera_config), 'msg': error}})
                    
                    if response['progress'] != -1:
                        return self.finish_json({'progress': response['progress']}) # timelapse already active
    
                    def on_make(response=None, error=None):
                        if error:
                            return self.finish_json({'error': 'Failed to make timelapse movie at %(url)s: %(msg)s.' % {
                                    'url': remote.pretty_camera_url(camera_config), 'msg': error}})
    
                        self.finish_json({'progress': -1})
                    
                    remote.make_timelapse_movie(camera_config, framerate, interval, group=group, callback=on_make)

                remote.check_timelapse_movie(camera_config, group=group, callback=on_status)

            else: # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth(admin=True)
    def delete_all(self, camera_id, group):
        logging.debug('deleting picture group %(group)s of camera %(id)s' % {
                'group': group, 'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            try:
                mediafiles.del_media_group(camera_config, group, 'picture')
                self.finish_json()
                
            except Exception as e:
                self.finish_json({'error': unicode(e)})

        elif utils.remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to delete picture group from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json()

            remote.del_media_group(camera_config, group=group, media_type='picture', callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    def try_finish(self, content):
        try:
            self.finish(content)
            
        except IOError as e:
            logging.warning('could not write response: %(msg)s' % {'msg': unicode(e)})


class MovieHandler(BaseHandler):
    @asynchronous
    def get(self, camera_id, op, filename=None):
        if camera_id is not None:
            camera_id = int(camera_id)
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')
        
        if op == 'list':
            self.list(camera_id)
            
        elif op == 'download':
            self.download(camera_id, filename)
        
        elif op == 'preview':
            self.preview(camera_id, filename)
        
        else:
            raise HTTPError(400, 'unknown operation')
    
    @asynchronous
    def post(self, camera_id, op, filename=None, group=None):
        if camera_id is not None:
            camera_id = int(camera_id)
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')
        
        if op == 'delete':
            self.delete(camera_id, filename)
        
        elif op == 'delete_all':
            self.delete_all(camera_id, group)
        
        else:
            raise HTTPError(400, 'unknown operation')
    
    @BaseHandler.auth()
    def list(self, camera_id):
        logging.debug('listing movies for camera %(id)s' % {'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            def on_media_list(media_list):
                if media_list is None:
                    return self.finish_json({'error': 'Failed to get movies list.'})

                self.finish_json({
                    'mediaList': media_list,
                    'cameraName': camera_config['@name']
                })
            
            mediafiles.list_media(camera_config, media_type='movie',
                    callback=on_media_list, prefix=self.get_argument('prefix', None))
        
        elif utils.remote_camera(camera_config):
            def on_response(remote_list=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to get movie list for %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json(remote_list)
            
            remote.list_media(camera_config, media_type='movie', prefix=self.get_argument('prefix', None), callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def download(self, camera_id, filename):
        logging.debug('downloading movie %(filename)s of camera %(id)s' % {
                'filename': filename, 'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            content = mediafiles.get_media_content(camera_config, filename, 'movie')
            
            pretty_filename = camera_config['@name'] + '_' + os.path.basename(filename)
            self.set_header('Content-Type', 'video/mpeg')
            self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')
            
            self.finish(content)
        
        elif utils.remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to download movie from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                pretty_filename = os.path.basename(filename) # no camera name available w/o additional request
                self.set_header('Content-Type', 'video/mpeg')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')
                
                self.finish(response)

            remote.get_media_content(camera_config, filename=filename, media_type='movie', callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def preview(self, camera_id, filename):
        logging.debug('previewing movie %(filename)s of camera %(id)s' % {
                'filename': filename, 'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            content = mediafiles.get_media_preview(camera_config, filename, 'movie',
                    width=self.get_argument('width', None),
                    height=self.get_argument('height', None))
            
            if content:
                self.set_header('Content-Type', 'image/jpeg')
                
            else:
                self.set_header('Content-Type', 'image/svg+xml')
                content = open(os.path.join(settings.STATIC_PATH, 'img', 'no-preview.svg')).read()
            
            self.finish(content)
        
        elif utils.remote_camera(camera_config):
            def on_response(content=None, error=None):
                if content:
                    self.set_header('Content-Type', 'image/jpeg')
                    
                else:
                    self.set_header('Content-Type', 'image/svg+xml')
                    content = open(os.path.join(settings.STATIC_PATH, 'img', 'no-preview.svg')).read()

                self.finish(content)
            
            remote.get_media_preview(camera_config, filename=filename, media_type='movie',
                    width=self.get_argument('width', None),
                    height=self.get_argument('height', None),
                    callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth(admin=True)
    def delete(self, camera_id, filename):
        logging.debug('deleting movie %(filename)s of camera %(id)s' % {
                'filename': filename, 'id': camera_id})
        
        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            try:
                mediafiles.del_media_content(camera_config, filename, 'movie')
                self.finish_json()
                
            except Exception as e:
                self.finish_json({'error': unicode(e)})

        elif utils.remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to delete movie from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json()

            remote.del_media_content(camera_config, filename=filename, media_type='movie', callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth(admin=True)
    def delete_all(self, camera_id, group):
        logging.debug('deleting movie group %(group)s of camera %(id)s' % {
                'group': group, 'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.local_motion_camera(camera_config):
            try:
                mediafiles.del_media_group(camera_config, group, 'movie')
                self.finish_json()
                
            except Exception as e:
                self.finish_json({'error': unicode(e)})

        elif utils.remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to delete movie group from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json()

            remote.del_media_group(camera_config, group=group, media_type='movie', callback=on_response)

        else: # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')


class LogHandler(BaseHandler):
    LOGS = {
        'motion': (os.path.join(settings.LOG_PATH, 'motion.log'),  'motion.log'),
    }

    @BaseHandler.auth(admin=True)
    def get(self, name):
        log = self.LOGS.get(name)
        if log is None:
            raise HTTPError(404, 'no such log')

        (path, filename) = log
        logging.debug('serving log file %s from %s' % (filename, path))

        self.set_header('Content-Type', 'text/plain')
        self.set_header('Content-Disposition', 'attachment; filename=' + filename + ';')

        with open(path) as f:
            self.finish(f.read())


class UpdateHandler(BaseHandler):
    @BaseHandler.auth(admin=True)
    def get(self):
        logging.debug('listing versions')
        
        versions = update.get_all_versions()
        current_version = update.get_version()
        update_version = None
        if versions and update.compare_versions(versions[-1], current_version) > 0:
            update_version = versions[-1]

        self.finish_json({
            'update_version': update_version,
            'current_version': current_version
        })

    @BaseHandler.auth(admin=True)
    def post(self):
        version = self.get_argument('version')
        
        logging.debug('performing update to version %(version)s' % {'version': version})
        
        result = update.perform_update(version)
        
        self.finish_json(result)


class PowerHandler(BaseHandler):
    @BaseHandler.auth(admin=True)
    def post(self, op):
        if op == 'shutdown':
            self.shut_down()
            
        elif op == 'reboot':
            self.reboot()
    
    def shut_down(self):
        IOLoop.instance().add_timeout(datetime.timedelta(seconds=2), powerctl.shut_down)

    def reboot(self):
        IOLoop.instance().add_timeout(datetime.timedelta(seconds=2), powerctl.reboot)


class VersionHandler(BaseHandler):
    def get(self):
        self.render('version.html',
                version=update.get_version(),
                hostname=socket.gethostname())

    post = get


# this will only trigger the login mechanism on the client side, if required 
class LoginHandler(BaseHandler):
    @BaseHandler.auth()
    def get(self):
        self.finish_json()

    def post(self):
        self.set_header('Content-Type', 'text/html')
        self.finish()
