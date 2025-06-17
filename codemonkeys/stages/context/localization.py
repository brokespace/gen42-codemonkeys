from dataclasses import dataclass
import re

from codemonkeys.trajectory_data.structs import (
    RelevanceDecision,
    ContextChunk,
    RelevantChunks,
)

from codemonkeys.swe_bench.swe_bench_verified import CodebaseFile, SWEBenchProblem
from codemonkeys.utils import count_tokens
import openai
import tqdm
from codemonkeys.utils.concurrency import parallelize
from codemonkeys.utils.shared_config import CodeMonkeysConfigWithLLM
import pydra
from codemonkeys.prompts.localization_prompts import (
    get_user_prompt,
    SYSTEM_PROMPT,
)
import anthropic
from copy import deepcopy


def create_context_chunks(
    message: str,
    locations: list[str],
    file_path: str,
    file_content: str,
    context_lines: int = 200,
) -> RelevantChunks:
    """
    Create context chunks from locations with surrounding lines.

    Args:
        locations: List of location strings in format "line: X" or "content: Y"
        file_path: Path to the file
        file_content: Content of the file
        context_lines: Number of lines to include before and after each location

    Returns:
        RelevantChunks object containing the context chunks
    """
    # Extract line numbers from locations
    line_numbers = set()
    for loc in locations:
        if loc.startswith("line:"):
            try:
                line_num = int(loc.split(":")[1].strip())
                line_numbers.add(line_num)
            except ValueError:
                continue

    if not line_numbers:
        return RelevantChunks(message=message, chunks=[], file_path=file_path)

    # Convert file content to lines
    lines = file_content.split("\n")

    # Create initial chunks with surrounding context
    chunks = []
    for line_num in sorted(line_numbers):
        # Ensure line number is within bounds
        if line_num < 1 or line_num > len(lines):
            continue

        # Calculate start and end lines for context
        start_line = max(1, line_num - context_lines)
        end_line = min(len(lines), line_num + context_lines)

        # Create chunk
        chunk = ContextChunk(
            line_number=start_line, content=lines[start_line - 1 : end_line]
        )
        chunks.append(chunk)

    # Combine overlapping chunks
    if not chunks:
        return RelevantChunks(message=message, chunks=[], file_path=file_path)

    combined_chunks = []
    current_chunk = chunks[0]

    for next_chunk in chunks[1:]:
        # Check if chunks overlap
        current_end = current_chunk.line_number + len(current_chunk.content)
        if next_chunk.line_number <= current_end:
            # Combine chunks
            current_chunk.content = lines[
                current_chunk.line_number
                - 1 : next_chunk.line_number
                + len(next_chunk.content)
                - 1
            ]
        else:
            # No overlap, add current chunk and start new one
            combined_chunks.append(current_chunk)
            current_chunk = next_chunk

    combined_chunks.append(current_chunk)

    return RelevantChunks(message=message, chunks=combined_chunks, file_path=file_path)


class LocalizationConfig(CodeMonkeysConfigWithLLM):
    def __init__(self):
        super().__init__()

        self.max_workers = 256
        self.timeout = 1800.0
        self.max_file_tokens = 100_000
        self.max_tokens = 10_000
        self.anthropic_cache = True
        self.model = "anthropic/claude-3.5-sonnet"
        self.model_type = "anthropic"
        self.api_key = ""


from codemonkeys.trajectory_data.structs import RelevantLocations


def _parse_response(message: str) -> RelevantLocations:
    """
    Parse the response from the LLM to extract the relevant locations.

    The expected format is:
    LOCATIONS:
    ```
    line: 10
    class: MyClass1
    line: 51
    content: def my_function():
    ```

    Returns a RelevantLocations object containing the parsed locations.
    """
    # Extract the locations section using regex
    locations_match = re.search(r"LOCATIONS:\s*\n```(.*?)```", message, re.DOTALL)

    if not locations_match:
        # If no locations found, return empty locations
        return RelevantLocations(message=message, locations=[])

    # Extract the content inside the code block
    locations_text = locations_match.group(1).strip()

    # If the locations section is empty, return empty locations
    if not locations_text:
        return RelevantLocations(message=message, locations=[])

    # Split by lines and filter out empty lines
    locations = [loc.strip() for loc in locations_text.split("\n") if loc.strip()]

    return RelevantLocations(message=message, locations=locations)


@dataclass
class WorkItem:
    problem: SWEBenchProblem
    file: CodebaseFile
    config: LocalizationConfig
    client: openai.OpenAI


# TODO move this
def call_completions(config: LocalizationConfig, messages, client):
    if isinstance(client, openai.OpenAI):
        resp = client.chat.completions.create(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            messages=messages,  # type: ignore
        )
        message = resp.choices[0].message.content
        assert message is not None, f"something's wrong with the response: {resp}"
        return message

    elif isinstance(client, anthropic.Anthropic):
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

        system_message = {"type": "text", "text": messages[0]["content"]}

        user_and_asst_messages: list[dict] = messages[1:]  # type: ignore
        if config.anthropic_cache:
            system_message["cache_control"] = {"type": "ephemeral"}  # type: ignore
            user_message_positions = [
                i for i, m in enumerate(user_and_asst_messages) if m["role"] == "user"
            ]

            # always add a cache block to the second most recent user message
            # to trigger a cache read.
            # if we're not on the last request of the state machine,
            # add a cache block to the most recent user message to trigger a cache write.

            positions_for_cache_control = user_message_positions[-2:]

            user_and_asst_messages = deepcopy(user_and_asst_messages)
            for pos in positions_for_cache_control:
                cur_content = user_and_asst_messages[pos]["content"]
                if isinstance(cur_content, str):
                    user_and_asst_messages[pos]["content"] = [
                        {
                            "type": "text",
                            "text": user_and_asst_messages[pos]["content"],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                else:
                    assert isinstance(cur_content, list)
                    cur_content[-1]["cache_control"] = {"type": "ephemeral"}

        resp = client.messages.create(
            model=config.model,
            messages=user_and_asst_messages,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            system=[system_message],
        )

        out = resp.content[0].text  # type: ignore
        assert isinstance(out, str)
        return out
    else:
        raise ValueError(f"Unknown client type: {type(client)}")


def _run_work(work: WorkItem):
    trajectory_store = work.config.get_trajectory_store(work.problem)

    if trajectory_store.localization_decision_exists(work.file.path):
        return

    prompt = get_user_prompt(work.problem, work.file.path, work.file.content)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    if count_tokens.count_tokens(work.file.content) > work.config.max_file_tokens:
        return

    response = call_completions(work.config, messages, work.config.client)
    message = response
    assert message is not None

    relevant_locations = _parse_response(message)

    relevant_chunks = create_context_chunks(
        message, relevant_locations.locations, work.file.path, work.file.content
    )

    trajectory_store.save_per_file_relevant_chunks(work.file.path, relevant_chunks)


def _should_include_file(file_path: str):
    if file_path.split(".")[-1] != "py":
        return False

    # heuristic to reduce the search space
    if file_path.split("/")[0] in ("test", "tests"):
        return False

    return True


@pydra.main(LocalizationConfig)
def main(config: LocalizationConfig):
    client = config.client
    client.api_key = config.api_key
    work = []
    for problem in tqdm.tqdm(
        config.get_problems(), desc="Submitting futures for localization"
    ):
        trajectory_store = config.get_trajectory_store(problem)
        files = trajectory_store.load_relevant_files()
        for file in files:
            if _should_include_file(file.path):
                work.append(
                    WorkItem(
                        problem=problem,
                        file=file,
                        config=config,
                        client=client,
                    )
                )

    parallelize(
        _run_work, work, config.max_workers, desc="Waiting for futures for localization"
    )


if __name__ == "__main__":
    main()
