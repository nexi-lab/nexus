"""ACE (Agentic Context Engineering) - Learning Engine.

Implements trajectory tracking, reflection, curation, and consolidation
for continuous agent learning.
"""

from nexus.core.ace.consolidation import ConsolidationEngine
from nexus.core.ace.curation import Curator
from nexus.core.ace.feedback import FeedbackManager
from nexus.core.ace.learning_loop import LearningLoop
from nexus.core.ace.playbook import PlaybookManager
from nexus.core.ace.reflection import Reflector
from nexus.core.ace.trajectory import TrajectoryManager

__all__ = [
    "TrajectoryManager",
    "Reflector",
    "Curator",
    "PlaybookManager",
    "ConsolidationEngine",
    "FeedbackManager",
    "LearningLoop",
]
