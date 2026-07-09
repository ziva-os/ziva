import re
from pathlib import Path

from ziva.shared_types import ToolResult, resolve_workspace_cwd


def levenshtein(a: str, b: str) -> int:
    if not a or not b:
        return max(len(a), len(b))
    matrix = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        matrix[i][0] = i
    for j in range(len(b) + 1):
        matrix[0][j] = j
        
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            matrix[i][j] = min(
                matrix[i - 1][j] + 1,
                matrix[i][j - 1] + 1,
                matrix[i - 1][j - 1] + cost
            )
    return matrix[len(a)][len(b)]


SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.65
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.65


def simple_replacer(content: str, find: str):
    yield find


def line_trimmed_replacer(content: str, find: str):
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
        
    for i in range(len(original_lines) - len(search_lines) + 1):
        matches = True
        for j in range(len(search_lines)):
            if original_lines[i + j].strip() != search_lines[j].strip():
                matches = False
                break
        if matches:
            match_start_index = sum(len(line) + 1 for line in original_lines[:i])
            match_end_index = match_start_index
            for k in range(len(search_lines)):
                match_end_index += len(original_lines[i + k])
                if k < len(search_lines) - 1:
                    match_end_index += 1
            yield content[match_start_index:match_end_index]


def block_anchor_replacer(content: str, find: str):
    original_lines = content.split("\n")
    search_lines = find.split("\n")
    
    if len(search_lines) < 3:
        return
        
    if search_lines and search_lines[-1] == "":
        search_lines.pop()
        
    first_line_search = search_lines[0].strip()
    last_line_search = search_lines[-1].strip()
    search_block_size = len(search_lines)
    max_line_delta = max(1, int(search_block_size * 0.25))
    
    candidates = []
    for i in range(len(original_lines)):
        if original_lines[i].strip() != first_line_search:
            continue
            
        for j in range(i + 2, len(original_lines)):
            if original_lines[j].strip() == last_line_search:
                actual_block_size = j - i + 1
                if abs(actual_block_size - search_block_size) <= max_line_delta:
                    candidates.append({"start_line": i, "end_line": j})
                break
                
    if not candidates:
        return
        
    if len(candidates) == 1:
        candidate = candidates[0]
        start_line = candidate["start_line"]
        end_line = candidate["end_line"]
        actual_block_size = end_line - start_line + 1
        
        similarity = 0
        lines_to_check = min(search_block_size - 2, actual_block_size - 2)
        
        if lines_to_check > 0:
            for j in range(1, min(search_block_size - 1, actual_block_size - 1)):
                original_line = original_lines[start_line + j].strip()
                search_line = search_lines[j].strip()
                max_len = max(len(original_line), len(search_line))
                if max_len == 0:
                    continue
                distance = levenshtein(original_line, search_line)
                similarity += (1 - distance / max_len) / lines_to_check
                if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                    break
        else:
            similarity = 1.0
            
        if similarity >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            match_start_index = sum(len(line) + 1 for line in original_lines[:start_line])
            match_end_index = match_start_index
            for k in range(start_line, end_line + 1):
                match_end_index += len(original_lines[k])
                if k < end_line:
                    match_end_index += 1
            yield content[match_start_index:match_end_index]
        return
        
    best_match = None
    max_similarity = -1
    
    for candidate in candidates:
        start_line = candidate["start_line"]
        end_line = candidate["end_line"]
        actual_block_size = end_line - start_line + 1
        
        similarity = 0
        lines_to_check = min(search_block_size - 2, actual_block_size - 2)
        
        if lines_to_check > 0:
            for j in range(1, min(search_block_size - 1, actual_block_size - 1)):
                original_line = original_lines[start_line + j].strip()
                search_line = search_lines[j].strip()
                max_len = max(len(original_line), len(search_line))
                if max_len == 0:
                    continue
                distance = levenshtein(original_line, search_line)
                similarity += (1 - distance / max_len)
            similarity /= lines_to_check
        else:
            similarity = 1.0
            
        if similarity > max_similarity:
            max_similarity = similarity
            best_match = candidate
            
    if max_similarity >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD and best_match:
        start_line = best_match["start_line"]
        end_line = best_match["end_line"]
        match_start_index = sum(len(line) + 1 for line in original_lines[:start_line])
        match_end_index = match_start_index
        for k in range(start_line, end_line + 1):
            match_end_index += len(original_lines[k])
            if k < end_line:
                match_end_index += 1
        yield content[match_start_index:match_end_index]


def whitespace_normalized_replacer(content: str, find: str):
    def normalize_whitespace(text: str) -> str:
        return re.sub(r'\s+', ' ', text).strip()
        
    normalized_find = normalize_whitespace(find)
    lines = content.split("\n")
    
    for line in lines:
        if normalize_whitespace(line) == normalized_find:
            yield line
        else:
            normalized_line = normalize_whitespace(line)
            if normalized_find in normalized_line:
                words = find.strip().split()
                if words:
                    pattern = r'\s+'.join(re.escape(w) for w in words)
                    try:
                        match = re.search(pattern, line)
                        if match:
                            yield match.group(0)
                    except re.error:
                        pass
                        
    find_lines = find.split("\n")
    if len(find_lines) > 1:
        for i in range(len(lines) - len(find_lines) + 1):
            block = "\n".join(lines[i:i + len(find_lines)])
            if normalize_whitespace(block) == normalized_find:
                yield block


def indentation_flexible_replacer(content: str, find: str):
    def remove_indentation(text: str) -> str:
        lines = text.split("\n")
        non_empty_lines = [line for line in lines if line.strip()]
        if not non_empty_lines:
            return text
            
        min_indent = min(len(line) - len(line.lstrip()) for line in non_empty_lines)
        return "\n".join(line if not line.strip() else line[min_indent:] for line in lines)
        
    normalized_find = remove_indentation(find)
    content_lines = content.split("\n")
    find_lines = find.split("\n")
    
    for i in range(len(content_lines) - len(find_lines) + 1):
        block = "\n".join(content_lines[i:i + len(find_lines)])
        if remove_indentation(block) == normalized_find:
            yield block


def escape_normalized_replacer(content: str, find: str):
    def unescape_string(s: str) -> str:
        def repl(match):
            c = match.group(1)
            mapping = {
                'n': '\n', 't': '\t', 'r': '\r', "'": "'", '"': '"',
                '`': '`', '\\': '\\', '\n': '\n', '$': '$'
            }
            return mapping.get(c, match.group(0))
        return re.sub(r'\\([ntr\'"`\\\n$])', repl, s)
        
    unescaped_find = unescape_string(find)
    if unescaped_find in content:
        yield unescaped_find
        
    lines = content.split("\n")
    find_lines = unescaped_find.split("\n")
    
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i:i + len(find_lines)])
        unescaped_block = unescape_string(block)
        if unescaped_block == unescaped_find:
            yield block


def trimmed_boundary_replacer(content: str, find: str):
    trimmed_find = find.strip()
    if trimmed_find == find:
        return
        
    if trimmed_find in content:
        yield trimmed_find
        
    lines = content.split("\n")
    find_lines = find.split("\n")
    
    for i in range(len(lines) - len(find_lines) + 1):
        block = "\n".join(lines[i:i + len(find_lines)])
        if block.strip() == trimmed_find:
            yield block


def context_aware_replacer(content: str, find: str):
    find_lines = find.split("\n")
    if len(find_lines) < 3:
        return
        
    if find_lines and find_lines[-1] == "":
        find_lines.pop()
        
    content_lines = content.split("\n")
    first_line = find_lines[0].strip()
    last_line = find_lines[-1].strip()
    
    for i in range(len(content_lines)):
        if content_lines[i].strip() != first_line:
            continue
            
        for j in range(i + 2, len(content_lines)):
            if content_lines[j].strip() == last_line:
                block_lines = content_lines[i:j + 1]
                block = "\n".join(block_lines)
                
                if len(block_lines) == len(find_lines):
                    matching_lines = 0
                    total_non_empty_lines = 0
                    
                    for k in range(1, len(block_lines) - 1):
                        block_line = block_lines[k].strip()
                        find_line = find_lines[k].strip()
                        
                        if block_line or find_line:
                            total_non_empty_lines += 1
                            if block_line == find_line:
                                matching_lines += 1
                                
                    if total_non_empty_lines == 0 or matching_lines / total_non_empty_lines >= 0.5:
                        yield block
                        break
                break


def multi_occurrence_replacer(content: str, find: str):
    start_index = 0
    while True:
        index = content.find(find, start_index)
        if index == -1:
            break
        yield find
        start_index = index + len(find)


def is_disproportionate_match(search: str, old_string: str) -> bool:
    old_lines = len(old_string.split("\n"))
    search_lines = len(search.split("\n"))
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    return len(search.strip()) > max(len(old_string.strip()) + 500, len(old_string.strip()) * 4)


def replace_text(content: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    if old_string == new_string:
        raise ValueError("No changes to apply: old_string and new_string are identical.")
    if old_string == "":
        raise ValueError("old_string cannot be empty when editing an existing file.")
        
    not_found = True
    
    replacers = [
        simple_replacer,
        line_trimmed_replacer,
        block_anchor_replacer,
        whitespace_normalized_replacer,
        indentation_flexible_replacer,
        escape_normalized_replacer,
        trimmed_boundary_replacer,
        context_aware_replacer,
        multi_occurrence_replacer
    ]
    
    for replacer in replacers:
        for search in replacer(content, old_string):
            index = content.find(search)
            if index == -1:
                continue
            not_found = False
            
            if is_disproportionate_match(search, old_string):
                raise ValueError("Refusing replacement because the matched span is much larger than old_string. Re-read the file and provide the full exact old_string for the intended replacement.")
                
            if replace_all:
                return content.replace(search, new_string)
                
            last_index = content.rfind(search)
            if index != last_index:
                continue
                
            return content[:index] + new_string + content[index + len(search):]
            
    if not_found:
        raise ValueError("Could not find old_string in the file. It must match exactly, including whitespace, indentation, and line endings.")
        
    raise ValueError("Found multiple matches for old_string. Provide more surrounding context to make the match unique.")


class EditFileTool:
    """Edit a file by replacing old_string with new_string.
    
    Mirrors opencode's robust multi-strategy replacement algorithm.
    """

    def spec(self):
        return {
            "name": "edit_file",
            "description": (
                "Edit a file by replacing old_string with new_string.\n"
                "Uses a robust multi-strategy search to find the exact text block to replace.\n"
                "You must provide enough surrounding context in old_string to ensure a unique match."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The absolute path to the file to modify, or relative to cwd.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The text to replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "The text to replace it with (must be different from old_string).",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences of old_string (default false).",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Current working directory for resolving relative paths (default: the session's workspace directory).",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        }

    async def run(self, input_data, ctx):
        file_path_str = input_data.get("file_path")
        if not file_path_str:
            return ToolResult(text="Error: file_path is required", error=True)
            
        old_string = input_data.get("old_string")
        new_string = input_data.get("new_string")
        
        if old_string is None or new_string is None:
            return ToolResult(text="Error: old_string and new_string are required", error=True)
            
        replace_all = bool(input_data.get("replace_all", False))
        
        cwd = input_data.get("cwd") or resolve_workspace_cwd(ctx)
        file_path = Path(cwd) / file_path_str
        
        if not file_path.exists():
            return ToolResult(text=f"Error: File {file_path} not found", error=True)
        if file_path.is_dir():
            return ToolResult(text=f"Error: Path is a directory, not a file: {file_path}", error=True)
            
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(text=f"Error: Could not read file {file_path}: {e}", error=True)

        try:
            ending = "\r\n" if "\r\n" in content else "\n"
            content_normalized = content.replace("\r\n", "\n")
            old_string_normalized = old_string.replace("\r\n", "\n")
            new_string_normalized = new_string.replace("\r\n", "\n")
            
            new_content_normalized = replace_text(
                content_normalized, old_string_normalized, new_string_normalized, replace_all
            )
            
            if ending == "\r\n":
                new_content = new_content_normalized.replace("\n", "\r\n")
            else:
                new_content = new_content_normalized
                
            file_path.write_text(new_content, encoding="utf-8")
            return ToolResult(text="Edit applied successfully.")
        except Exception as e:
            return ToolResult(text=f"Error: {str(e)}", error=True)
