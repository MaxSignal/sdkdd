import hashlib
import os
from ..utils import trace_unhandled_exceptions, remove_suffix, remove_prefix
import config
import psycopg2
import pathlib
import datetime
import magic
import re
import mimetypes
import requests
from psycopg2.extras import RealDictCursor
from retry import retry

@trace_unhandled_exceptions
@retry(tries=5)
def migrate_inline(path, migration_id):
    # check if the file is special (symlink, hardlink, empty) and return if so
    if os.path.islink(path) or os.path.getsize(path) == 0 or os.path.ismount(path):
        return
    
    if config.ignore_temp_files and path.endswith('.temp'):
        return
    
    file_ext = os.path.splitext(path)[1]
    web_path = path.replace(remove_suffix(config.data_dir, '/'), '')
    service = None
    post_id = None
    user_id = None
    with open(path, 'rb') as f:
        # get hash and filename
        file_hash_raw = hashlib.sha256()
        for chunk in iter(lambda: f.read(8192), b''):
            file_hash_raw.update(chunk)
        file_hash = file_hash_raw.hexdigest()
        new_filename = os.path.join('/', file_hash[0:2], file_hash[2:4], file_hash)
        
        mime = magic.from_file(path, mime=True)
        if (config.fix_extensions):
            file_ext = mimetypes.guess_extension(mime or 'application/octet-stream', strict=False)
            new_filename = new_filename + (re.sub('^.jpe$', '.jpg', file_ext) if config.fix_jpe else file_ext)
        else:
            new_filename = new_filename + (re.sub('^.jpe$', '.jpg', file_ext) if config.fix_jpe else file_ext)
        
        fname = pathlib.Path(path)
        mtime = datetime.datetime.fromtimestamp(fname.stat().st_mtime)
        ctime = datetime.datetime.fromtimestamp(fname.stat().st_ctime)

        conn = psycopg2.connect(
            host = config.database_host,
            dbname = config.database_dbname,
            user = config.database_user,
            password = config.database_password,
            port = 5432,
            cursor_factory=RealDictCursor
        )

        # log to file tracking table
        if (not config.dry_run):
            cursor = conn.cursor()
            cursor.execute("INSERT INTO files (hash, mtime, ctime, mime, ext) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (hash) DO UPDATE SET hash = EXCLUDED.hash RETURNING id", (file_hash, mtime, ctime, mime, file_ext))
            file_id = cursor.fetchone()['id']

        updated_rows = 0
        # update "inline" path references in db, using different strategies to speed the operation up
        # strat 1: attempt to scope out posts archived up to 1 hour after the file was modified (kemono data should almost never change)
        step = 1
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE posts SET content = replace(content, %s, %s) WHERE added >= %s AND added < %s AND content LIKE %s RETURNING posts.id, posts.service, posts.\"user\";",
            (web_path, new_filename, mtime, mtime + datetime.timedelta(hours=1), f'%{web_path}%')
        )
        updated_rows = cursor.rowcount
        post = cursor.fetchone()
        if (post):
            service = post['service']
            user_id = post['user']
            post_id = post['id']
        cursor.close()
        
        # NOTE: Check if filename is integer and use that for added time optimization.
        # optimizations didn't work, simply find and replace references in inline text.
        # ... this will take a very long time.
        if updated_rows == 0:
            step = 2
            cursor = conn.cursor()
            cursor.execute("UPDATE posts SET content = replace(content, %s, %s) WHERE content LIKE %s RETURNING posts.id, posts.service, posts.\"user\";", (web_path, new_filename, f'%{web_path}%'))
            updated_rows = cursor.rowcount
            post = cursor.fetchone()
            if (post):
                service = post['service']
                user_id = post['user']
                post_id = post['id']
            cursor.close()
        
        # log file post relationship (not discord)
        if (not config.dry_run and updated_rows > 0 and service and user_id and post_id):
            cursor = conn.cursor()
            cursor.execute("INSERT INTO file_post_relationships (file_id, filename, service, \"user\", post, inline) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (file_id, os.path.basename(path), service, user_id, post_id, True))

        # log to sdkdd_migration_{migration_id} (see sdkdd.py for schema)
        # log to general file tracking table (schema: serial id, hash, filename, locally stored path, remotely stored path?, last known mtime, last known ctime, extension, mimetype, service, user, post, contributor_user?)
        if (not config.dry_run):
            cursor = conn.cursor()
            cursor.execute(f"INSERT INTO sdkdd_migration_{migration_id} (old_location, new_location, ctime, mtime) VALUES (%s, %s, %s, %s)", (web_path, new_filename, mtime, ctime))
            cursor.close()

        # commit db
        if (config.dry_run):
            conn.rollback()
        else:
            conn.commit()
        
        if (not config.dry_run):
            new_filename_without_prefix = remove_prefix(new_filename, '/')
            web_path_without_prefix = remove_prefix(web_path, '/')
            # move to hashy location, do nothing if something is already there
            # move thumbnail to hashy location
            thumb_dir = config.thumb_dir or os.path.join(config.data_dir, 'thumbnail')
            if os.path.isfile(os.path.join(thumb_dir, web_path_without_prefix)) and not os.path.isfile(os.path.join(thumb_dir, new_filename_without_prefix)):
                os.makedirs(os.path.join(thumb_dir, file_hash[0:2], file_hash[2:4]), exist_ok=True)
                os.rename(os.path.join(thumb_dir, web_path_without_prefix), os.path.join(thumb_dir, new_filename_without_prefix))
            
            if os.path.isfile(path) and not os.path.isfile(os.path.join(config.data_dir, new_filename_without_prefix)):
                os.makedirs(os.path.join(config.data_dir, file_hash[0:2], file_hash[2:4]), exist_ok=True)
                os.rename(path, os.path.join(config.data_dir, new_filename_without_prefix))

        if (not config.dry_run and config.ban_url and service and user_id):
            requests.request('BAN', f"{config.ban_url}/{service}/user/" + user_id)
        
        conn.close()
        
        # done!
        if (service and user_id and post_id):
            print(f'{web_path} -> {new_filename} ({updated_rows} database entries updated; {service}/{user_id}/{post_id}, found at step {step})')
        else:
            print(f'{web_path} -> {new_filename} ({updated_rows} database entries updated; no post/messages found)')