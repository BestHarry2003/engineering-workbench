# Executed by Abaqus/CAE noGUI, compatible with Abaqus Python 2 and 3.
from __future__ import print_function
import io
import json
import os
from abaqus import openMdb
from abaqusConstants import ON


def main():
    with io.open('request.json', 'r', encoding='utf-8') as stream:
        request = json.load(stream)
    result = {}
    database = None
    try:
        # The caller supplies a disposable copy; the original CAE is never saved.
        database = openMdb(pathName=request['cae'])
        names = sorted(database.jobs.keys())
        result['jobs'] = names
        if request.get('job'):
            name = request['job']
            if name not in names:
                raise ValueError('Job does not exist: ' + name)
            database.jobs[name].writeInput(consistencyChecking=ON)
            result['input'] = os.path.abspath(name + '.inp')
    except Exception as error:
        result = {'error': str(error)}
    finally:
        if database is not None:
            database.close()
        with io.open('result.json', 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=True))


main()
