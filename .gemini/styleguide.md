# Code Review Guidance

## Overview  
This codebase frequently encounters risks related to configuration flexibility, inheritance complexity, and cross-system consistency. Tight coupling between components, ambiguous parameter handling, and platform-specific assumptions often lead to subtle bugs in distributed workflows. A structured review approach ensures robustness across deployment scenarios, prevents technical debt from implicit dependencies, and maintains API coherence as new modules integrate with existing systems.

## Hard requirements (MUST)

- MUST write all review comments in Chinese.
