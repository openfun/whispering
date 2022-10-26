#!/usr/bin/env python3

import argparse
import asyncio
import queue
import sys
from enum import Enum
from logging import DEBUG, INFO, basicConfig, getLogger
from pathlib import Path
from typing import Iterator, List, Optional, Union

import sounddevice as sd
import torch
from whisper import available_models
from whisper.audio import N_FRAMES, SAMPLE_RATE
from whisper.tokenizer import LANGUAGES, TO_LANGUAGE_CODE

from whispering.pbar import ProgressBar
from whispering.schema import (
    CURRENT_PROTOCOL_VERSION,
    Context,
    StdoutWriter,
    WhisperConfig,
)
from whispering.serve import serve_with_websocket
from whispering.transcriber import WhisperStreamingTranscriber
from whispering.websocket_client import run_websocket_client

logger = getLogger(__name__)


class Mode(Enum):
    client = "client"
    server = "server"
    mic = "mic"

    def __str__(self):
        return self.value


def transcribe_from_mic(
    *,
    wsp: WhisperStreamingTranscriber,
    sd_device: Optional[Union[int, str]],
    num_block: int,
    ctx: Context,
    no_progress: bool,
) -> Iterator[str]:
    q = queue.Queue()

    def sd_callback(indata, frames, time, status):
        if status:
            logger.warning(status)
        q.put(indata.ravel())

    logger.info("Ready to transcribe")
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=N_FRAMES * num_block,
        device=sd_device,
        dtype="float32",
        channels=1,
        callback=sd_callback,
    ):
        idx: int = 0
        while True:
            logger.debug(f"Audio #: {idx}, The rest of queue: {q.qsize()}")

            if no_progress:
                audio = q.get()
            else:
                pbar_thread = ProgressBar(
                    num_block=num_block,  # TODO: set more accurate value
                )
                try:
                    audio = q.get()
                except KeyboardInterrupt:
                    pbar_thread.kill()
                    return
                pbar_thread.kill()

            logger.debug(f"Got. The rest of queue: {q.qsize()}")
            if not no_progress:
                sys.stderr.write("Analyzing")
                sys.stderr.flush()

            for chunk in wsp.transcribe(audio=audio, ctx=ctx):
                if not no_progress:
                    sys.stderr.write("\r")
                    sys.stderr.flush()
                yield f"{chunk.start:.2f}->{chunk.end:.2f}\t{chunk.text}\n"
                if not no_progress:
                    sys.stderr.write("Analyzing")
                    sys.stderr.flush()
            idx += 1
            if not no_progress:
                sys.stderr.write("\r")
                sys.stderr.flush()


def get_opts() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    group_model = parser.add_argument_group("Whisper model options")
    group_model.add_argument(
        "--model",
        type=str,
        choices=available_models(),
    )
    group_model.add_argument(
        "--language",
        type=str,
        choices= ["multilanguage"] + sorted(LANGUAGES.keys())
        + sorted([k.title() for k in TO_LANGUAGE_CODE.keys()]),
    )
    group_model.add_argument(
        "--device",
        default="cuda" if torch.cuda.is_available() else "cpu",
        help="device to use for PyTorch inference",
    )

    group_ws = parser.add_argument_group("WebSocket options")
    group_ws.add_argument(
        "--host",
        help="host of websocker server",
    )
    group_ws.add_argument(
        "--port",
        type=int,
        help="Port number of websocker server",
    )

    group_ctx = parser.add_argument_group("Parsing options")
    group_ctx.add_argument(
        "--beam_size",
        "-b",
        type=int,
        default=5,
    )
    group_ctx.add_argument(
        "--temperature",
        "-t",
        type=float,
        action="append",
        default=[],
    )
    group_ctx.add_argument(
        "--vad",
        type=float,
        help="Threshold of VAD",
        default=0.5,
    )
    group_ctx.add_argument(
        "--max_nospeech_skip",
        type=int,
        help="Maximum number of skip to analyze because of nospeech",
        default=16,
    )

    group_misc = parser.add_argument_group("Other options")
    group_misc.add_argument(
        "--output",
        "-o",
        help="Output file",
        type=Path,
        default=StdoutWriter(),
    )
    group_misc.add_argument(
        "--mic",
        help="Set MIC device",
    )
    group_misc.add_argument(
        "--num_block",
        "-n",
        type=int,
        default=20,
        help="Number of operation unit",
    )
    group_misc.add_argument(
        "--mode",
        choices=[v.value for v in Mode],
    )
    group_misc.add_argument(
        "--no-progress",
        action="store_true",
    )
    group_misc.add_argument(
        "--show-devices",
        action="store_true",
        help="Show MIC devices",
    )
    group_misc.add_argument(
        "--debug",
        action="store_true",
    )

    opts = parser.parse_args()

    if opts.beam_size <= 0:
        opts.beam_size = None
    if len(opts.temperature) == 0:
        opts.temperature = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    opts.temperature = sorted(set(opts.temperature))

    try:
        opts.mic = int(opts.mic)
    except Exception:
        pass
    return opts


def get_wshiper(*, opts) -> WhisperStreamingTranscriber:
    config = WhisperConfig(
        model_name=opts.model,
        language=opts.language,
        device=opts.device,
    )

    logger.debug(f"WhisperConfig: {config}")
    wsp = WhisperStreamingTranscriber(config=config)
    return wsp


def get_context(*, opts) -> Context:
    ctx = Context(
        protocol_version=CURRENT_PROTOCOL_VERSION,
        beam_size=opts.beam_size,
        temperatures=opts.temperature,
        max_nospeech_skip=opts.max_nospeech_skip,
        vad_threshold=opts.vad,
    )
    logger.debug(f"Context: {ctx}")
    return ctx


def show_devices():
    devices = sd.query_devices()
    for i, device in enumerate(devices):
        if device["max_input_channels"] > 0:
            print(f"{i}: {device['name']}")


def is_valid_arg(
    *,
    args: List[str],
    mode: str,
) -> bool:
    keys = []
    if mode == Mode.server.value:
        keys = {
            "--mic",
            "--beam_size",
            "-b",
            "--temperature",
            "-t",
            "--num_block",
            "-n",
            "--vad",
            "--max_nospeech_skip",
            "--output",
            "--show-devices",
            "--no-progress",
        }
    elif mode == Mode.mic.value:
        keys = {
            "--host",
            "--port",
        }

    for arg in args:
        if arg in keys:
            sys.stderr.write(f"{arg} is not accepted option for {mode} mode\n")
            return False
    return True


def main() -> None:
    opts = get_opts()

    basicConfig(
        level=DEBUG if opts.debug else INFO,
        format="[%(asctime)s] %(module)s.%(funcName)s:%(lineno)d %(levelname)s -> %(message)s",
    )

    if opts.show_devices:
        return show_devices()

    if (
        opts.host is not None
        and opts.port is not None
        and opts.mode != Mode.client.value
    ):
        opts.mode = Mode.server.value

    if not is_valid_arg(
        args=sys.argv[1:],
        mode=opts.mode,
    ):
        sys.exit(1)

    if opts.mode == Mode.client.value:
        assert opts.language is None
        assert opts.model is None
        ctx: Context = get_context(opts=opts)
        try:
            asyncio.run(
                run_websocket_client(
                    sd_device=opts.mic,
                    num_block=opts.num_block,
                    host=opts.host,
                    port=opts.port,
                    no_progress=opts.no_progress,
                    ctx=ctx,
                    path_out=opts.output,
                )
            )
        except KeyboardInterrupt:
            pass
    elif opts.mode == Mode.server.value:
        assert opts.language is not None
        assert opts.model is not None
        wsp = get_wshiper(opts=opts)
        asyncio.run(
            serve_with_websocket(
                wsp=wsp,
                host=opts.host,
                port=opts.port,
            )
        )
    else:
        assert opts.language is not None
        assert opts.model is not None
        wsp = get_wshiper(opts=opts)
        ctx: Context = get_context(opts=opts)
        with opts.output.open("w") as outf:
            for text in transcribe_from_mic(
                wsp=wsp,
                sd_device=opts.mic,
                num_block=opts.num_block,
                no_progress=opts.no_progress,
                ctx=ctx,
            ):
                outf.write(text)
                outf.flush()


if __name__ == "__main__":
    main()
