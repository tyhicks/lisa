# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from dataclasses import InitVar, dataclass, field

from dataclasses_json import dataclass_json

HYPERV = "hyperv"


@dataclass_json()
@dataclass
class HyperVServerSchema:
    ...


@dataclass_json()
@dataclass
class HyperVPlatformSchema:
    ...
