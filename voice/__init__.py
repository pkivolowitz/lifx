"""GlowUp Voice Interface — wake word detection, STT, intent parsing, TTS.

Distributed voice control system:
- Satellites (Pi 3B + ReSpeaker or laptop mic) detect wake word and
  capture utterances.
- Coordinator (Daedalus M1 Studio) runs the processing pipeline:
  STT → intent parsing → GlowUp API execution → TTS → AirPlay response.

See ``voice/constants.py`` for MQTT topics and audio parameters.
See ``voice/protocol.py`` for the wire format between satellites and
coordinator.
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"
