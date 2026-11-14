"""Inspect public model metadata and a download error without credentials."""
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def inspect():

    api = 'https://civitai.com/api/v1/model-versions/3462708'
    try:
        with urlopen(Request(api), timeout=30) as response:
            version = json.load(response)
    except HTTPError as error:
        return {'api_error': {'status': error.code, 'content_type': error.headers.get('Content-Type'),
                              'server': error.headers.get('Server'),
                              'body': error.read(1000).decode(errors='replace')}}
    result = {'version_id': version['id'], 'model_id': version['modelId'],
              'files': [{key: file.get(key) for key in ['id', 'name', 'sizeKB', 'downloadUrl', 'hashes']}
                        for file in version['files']],
              'availability': version.get('availability'), 'earlyAccessEndsAt': version.get('earlyAccessEndsAt')}
    try:
        with urlopen(Request(result['files'][0]['downloadUrl'], headers={'Range': 'bytes=0-1023'}), timeout=30) as response:
            result['range_status'] = response.status
            result['content_range'] = response.headers.get('Content-Range')
    except HTTPError as error:
        result['range_error'] = {'status': error.code, 'content_type': error.headers.get('Content-Type'),
                                 'server': error.headers.get('Server'),
                                 'body': error.read(1000).decode(errors='replace')}
    return result


if __name__ == '__main__':
    result = inspect()
    path = Path(__file__).parents[2] / '.local/remote-acceptance/source-access.json'
    path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
