"""The analysis package: the analyses the build guide's tier lists name, one registry, one gate in front.

**Importing this package registers every analysis.** The registry is populated
by decorator at import time, so `catalogue()` is only as complete as whatever
happened to be imported first. Measured in Step 9: a tool layer that imported
`registry` and three analysis modules reported three available analyses in its
unknown-type refusal -- and the Phase 8 Done-When is that an unknown type
returns THE valid list. A list missing six of nine is a wrong answer that reads
like a right one.

So the modules are imported here, for their registration side effect, and
nothing else has to remember to. Ordered by tier, then by name.
"""

from __future__ import annotations

from . import cross_tab as _cross_tab  # noqa: F401
from . import distribution as _distribution  # noqa: F401
from . import frequency as _frequency  # noqa: F401
from . import summary_stats as _summary_stats  # noqa: F401
from . import group_compare as _group_compare  # noqa: F401
from . import pareto as _pareto  # noqa: F401
from . import ranking_shift as _ranking_shift  # noqa: F401
from . import calendar_coverage as _calendar_coverage  # noqa: F401
from . import trend as _trend  # noqa: F401
from . import seasonality as _seasonality  # noqa: F401
from . import period_compare as _period_compare  # noqa: F401
from . import growth_decomposition as _growth_decomposition  # noqa: F401
from . import correlation as _correlation  # noqa: F401
from . import bivariate as _bivariate  # noqa: F401
from . import driver_analysis as _driver_analysis  # noqa: F401
from . import mix_shift as _mix_shift  # noqa: F401
from . import outlier_detection as _outlier_detection  # noqa: F401
from . import changepoint as _changepoint  # noqa: F401
from . import correlated_shift as _correlated_shift  # noqa: F401
from . import hypothesis_test as _hypothesis_test  # noqa: F401
from . import confidence_interval as _confidence_interval  # noqa: F401
from . import effect_size as _effect_size  # noqa: F401
from . import sample_adequacy as _sample_adequacy  # noqa: F401
from . import repeat_behaviour as _repeat_behaviour  # noqa: F401
from . import cohort_retention as _cohort_retention  # noqa: F401
