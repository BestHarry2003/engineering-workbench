"""Lossless text normalization for PyroSim FDS import (not a solver/PSM writer).

Never drop unknown records/parameters or guess physical defaults. Files stay next
to the source so relative references retain their meaning. Import validation is
still performed by PyroSim, whose feature coverage depends on its version.
"""
import codecs
import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 32 * 1024 * 1024
NAMELIST = re.compile(r'[&$]([A-Za-z][A-Za-z0-9_]*)')
TOKEN = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
ASSIGNMENT = re.compile(r'\s*(?:\([^()\n]*\)\s*)?=')
KNOWN_RECORDS = set('HEAD TIME DUMP MESH MULT TRNX TRNY TRNZ OBST HOLE VENT SURF MATL REAC SPEC PART INIT DEVC CTRL RAMP TABL PROP HVAC SLCF BNDF BNDE ISOF PROF DUCT ZONE PRES MISC RADI COMB CLIP WIND CATF GEOM MOVE TAIL'.split())
IMPORT_HELP = '在 PyroSim 中选择「文件 → 导入 FDS/CAD 文件」，选择下面的 FDS 文件；检查导入警告后另存为 PSM。不要用“打开 PSM”读取 FDS。'


def import_findings(text):
    """Narrow, observed importer warnings; never a general compatibility verdict.

    Mask quoted values and comments before inspecting assignment names, so words
    in a TITLE or a comment cannot trigger a false parameter warning.
    """
    masked = list(text)
    i = 0
    while i < len(text):
        if text[i] == '!':
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            masked[i:end] = ' ' * (end - i)
            i = end; continue
        if text[i] in "\"'":
            quote, start = text[i], i
            i += 1
            while i < len(text):
                if text[i] == quote:
                    if i + 1 < len(text) and text[i+1] == quote:
                        i += 2; continue
                    i += 1; break
                i += 1
            masked[start:i] = ['\n' if c == '\n' else ' ' for c in text[start:i]]
            continue
        i += 1
    clean = ''.join(masked)
    known = {('DUMP', 'DT_SMOKE3D'), ('REAC', 'N_SIMPLE_CHEMISTRY_REACTIONS'), ('REAC', 'FUEL_C_TO_CO_FRACTION')}
    findings = []
    record = None
    line, cursor = 1, 0
    for match in re.finditer(r'&([A-Za-z][A-Za-z0-9_]*)|/|([A-Za-z_][A-Za-z0-9_]*)\s*=', clean):
        line += clean.count('\n', cursor, match.start()); cursor = match.start()
        if match.group(1): record = match.group(1).upper()
        elif match.group(0) == '/': record = None
        elif (record, match.group(2).upper()) in known:
            name = match.group(2).upper()
            findings.append({'record':record, 'parameter':name, 'line':line,
                             'testedVersion':'2024.1.0702', 'action':'preserve',
                             'message':f'第 {line} 行 {record}.{name}：PyroSim 2024.1.0702 实测会提示 Unknown property；样例导入后该字段仍保留在生成记录中。不要为消除警告删除它；请在 Record View 核对值，再另存 PSM。格式转换不会消除此警告。'})
    return findings


def source_bytes(raw_path):
    if not isinstance(raw_path, str) or not raw_path or any(ord(c) < 32 for c in raw_path):
        raise ValueError('请选择本机 FDS 文件')
    path = Path(raw_path).resolve()
    if path.suffix.lower() != '.fds' or not path.is_file():
        raise ValueError('请选择存在的 .fds 文件')
    with path.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('此转换器仅处理不超过 32 MB 的 FDS 文本')
    return path, data


def decode_source(data):
    for bom, encoding in [(codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
                          (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16'),
                          (codecs.BOM_UTF8, 'utf-8-sig')]:
        if data.startswith(bom):
            return data.decode(encoding), encoding
    try:
        return data.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError:
        try:
            return data.decode('gb18030'), 'gb18030'
        except UnicodeDecodeError as error:
            raise ValueError('无法识别文本编码，请先用文本编辑器另存为 UTF-8') from error


def normalize(text):
    """Scan quoted strings/comments first; normalize only namelist syntax.

    Keep comments, order, all values and all parameters verbatim. Doubled quotes
    are Fortran string escapes. Slash in a path or comment is not a terminator.
    """
    output, changes, errors, records = [], Counter(), [], []
    i, line, record = 0, 1, None
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    while i < len(text):
        char = text[i]
        if char == '!':
            end = text.find('\n', i)
            end = len(text) if end < 0 else end
            output.append(text[i:end]); i = end; continue
        if char in "\"'" and record:
            start, start_line, quote = i, line, char
            i += 1
            while i < len(text):
                if text[i] == quote:
                    if i + 1 < len(text) and text[i + 1] == quote:
                        i += 2; continue
                    i += 1; break
                if text[i] == '\n': line += 1
                i += 1
            else:
                errors.append(f'第 {start_line} 行：字符串引号未闭合，未自动猜测内容')
            output.append(text[start:i]); continue
        if char in '&$':
            match = NAMELIST.match(text, i)
            if match:
                token, name = match.group(0), match.group(1).upper()
                if record and name == 'END':
                    output.append('/'); changes['将 namelist END 结束标记转为 /'] += 1
                    record = None
                elif record:
                    errors.append(f'第 {line} 行：{record} 记录未结束就遇到 {name}')
                    output.append(token)
                else:
                    if name == 'END': errors.append(f'第 {line} 行：孤立的 END 标记')
                    record = name
                    records.append({'name':name, 'line':line})
                    canonical = '&' + name
                    output.append(canonical)
                    if token != canonical: changes['规范 namelist 名称及起始符'] += 1
                i += len(token); continue
            if record: errors.append(f'第 {line} 行：无法识别的 namelist 标记 {char}')
        if char == '/' and record:
            record = None
        elif record and (char.isascii() and (char.isalpha() or char == '_')):
            match = TOKEN.match(text, i)
            token = match.group(0)
            # Array indices are kept unchanged; only assignment keys are cased.
            if ASSIGNMENT.match(text, i + len(token)):
                canonical = token.upper()
                if canonical != token: changes['将参数名称规范为大写'] += 1
                token = canonical
            output.append(token); i += len(token); continue
        elif record and char in '“”‘’，；＝':
            errors.append(f'第 {line} 行：字段外出现全角符号 {char}，请确认其含义后修改')
        if ord(char) < 32 and char not in '\n\t':
            errors.append(f'第 {line} 行：包含不支持的控制字符 U+{ord(char):04X}')
        output.append(char)
        if char == '\n': line += 1
        i += 1
    if record: errors.append(f'文件结束时 {record} 记录缺少 / 结束符')
    if not records: errors.append('未找到 FDS namelist 记录')
    names = [r['name'] for r in records]
    for name in ('HEAD', 'TAIL'):
        if names.count(name) > 1: errors.append(f'存在多个 {name} 记录，请检查是否拼接了多份文件')
    if 'TAIL' in names and names[-1] != 'TAIL': errors.append('TAIL 后仍有模型记录，未自动删除或重排')
    return ''.join(output), changes, list(dict.fromkeys(errors)), records


def analyze(raw_path):
    path, data = source_bytes(raw_path)
    try:
        original, encoding = decode_source(data)
    except UnicodeError as error:
        raise ValueError('文件编码损坏，请检查源文件') from error
    normalized, changes, errors, records = normalize(original)
    findings = import_findings(normalized)
    if encoding != 'utf-8': changes['转为无 BOM 的 UTF-8 编码（保留中文）'] += 1
    if '\r' in original: changes['统一文本换行'] += 1
    warnings = ['这是文本兼容性检查，不是 PyroSim 导入验证或 FDS 求解验证；不删除未知字段，不调整网格、材料、边界或火源。']
    if encoding == 'gb18030': warnings.append('输入按 GB18030 解码，请核对预览中的中文名称；若乱码请停止转换。')
    names = {r['name'] for r in records}
    for name in sorted(names - KNOWN_RECORDS):
        warnings.append(f'{name}：此转换器未识别的记录，原样保留，请核对目标 PyroSim 的支持情况。')
    if 'GEOM' in names: warnings.append('含 GEOM 几何：请核对 PyroSim 的 GEOM 支持/首选项；不会自动改成 OBST 或删去几何。')
    if not {'HEAD', 'MESH', 'TAIL'} <= names: warnings.append('缺少 HEAD、MESH 或 TAIL 中的部分记录；可能是片段文件，转换器不会补造模型内容。')
    warnings.append('高版本参数或旧版模型语义无法仅靠文本格式转换修复；导入仍报错时请保留报错记录并核对版本。')
    warnings.extend(item['message'] for item in findings)
    report = {'source':str(path), 'sha256':hashlib.sha256(data).hexdigest(), 'encoding':encoding,
              'canConvert':not errors, 'validatedByPyroSim':False, 'compatibilityFindings':findings,
              'records':dict(Counter(r['name'] for r in records)), 'recordCount':len(records),
              'changes':[f'{key}（{count} 处）' for key,count in changes.items()],
              'errors':errors, 'warnings':warnings, 'preview':normalized[:6000], 'instructions':IMPORT_HELP}
    return report, normalized


def convert(raw_path, expected_sha256):
    report, text = analyze(raw_path)
    if not expected_sha256 or report['sha256'] != expected_sha256:
        raise ValueError('源文件已变化或尚未检查，请重新检查后再转换')
    if not report['canConvert']:
        raise ValueError('文件结构存在错误，请先处理检查报告中的错误')
    source = Path(report['source'])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    target = source.with_name(f'{source.stem}.pyrosim-{stamp}-{uuid.uuid4().hex[:6]}.fds')
    report.update(output=str(target), reportPath=str(target.with_suffix('.conversion.json')),
                  createdAt=datetime.now(timezone.utc).isoformat())
    # Exclusive creation: neither originals nor previous conversions are replaced.
    with target.open('xb') as stream:
        stream.write(text.encode('utf-8'))
    try:
        with Path(report['reportPath']).open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    except OSError:
        # Only remove the new file created by this invocation, never the source.
        target.unlink(missing_ok=True)
        raise
    return report
