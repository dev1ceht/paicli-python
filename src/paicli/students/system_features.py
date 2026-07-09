"""System features and capabilities for the student management system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Feature(Enum):
    """Feature flags for the student management system."""

    # Core CRUD features
    STUDENT_REGISTRATION = "student_registration"
    STUDENT_LOOKUP = "student_lookup"
    STUDENT_UPDATE = "student_update"
    STUDENT_DEACTIVATION = "student_deactivation"

    # Course management
    COURSE_MANAGEMENT = "course_management"
    COURSE_SCHEDULING = "course_scheduling"
    PREREQUISITE_CHECKING = "prerequisite_checking"

    # Enrollment features
    COURSE_ENROLLMENT = "course_enrollment"
    ENROLLMENT_WAITLIST = "enrollment_waitlist"
    BULK_ENROLLMENT = "bulk_enrollment"
    ENROLLMENT_DROPPING = "enrollment_dropping"

    # Grading features
    GRADE_RECORDING = "grade_recording"
    GRADE_CALCULATION = "grade_calculation"
    GPA_CALCULATION = "gpa_calculation"
    TRANSCRIPT_GENERATION = "transcript_generation"

    # Reporting & analytics
    STUDENT_REPORTING = "student_reporting"
    ENROLLMENT_ANALYTICS = "enrollment_analytics"
    ACADEMIC_PROGRESS_TRACKING = "academic_progress_tracking"

    # System features
    AUDIT_LOGGING = "audit_logging"
    NOTIFICATIONS = "notifications"
    DATA_EXPORT = "data_export"
    DATA_IMPORT = "data_import"


@dataclass(slots=True)
class SystemCapability:
    """Describes a specific capability the system provides.

    Attributes:
        name: Capability name.
        description: Human-readable description.
        feature: The feature flag this capability belongs to.
        enabled: Whether this capability is currently active.
    """

    name: str
    description: str
    feature: Feature
    enabled: bool = True


@dataclass(slots=True)
class StudentSystem:
    """Represents the student management system configuration.

    Attributes:
        name: Name of the system instance.
        version: System version string.
        enabled_features: Set of features that are currently enabled.
        capabilities: List of all system capabilities.
        config: Key-value configuration dictionary.
    """

    name: str
    version: str = "1.0.0"
    enabled_features: set[Feature] = field(default_factory=set)
    capabilities: list[SystemCapability] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def is_feature_enabled(self, feature: Feature) -> bool:
        """Check if a specific feature is enabled."""
        return feature in self.enabled_features

    def get_capabilities(self, feature: Feature | None = None) -> list[SystemCapability]:
        """Get capabilities, optionally filtered by feature."""
        if feature is None:
            return list(self.capabilities)
        return [cap for cap in self.capabilities if cap.feature == feature]

    def enable_feature(self, feature: Feature) -> None:
        """Enable a feature and its associated capabilities."""
        self.enabled_features.add(feature)
        for cap in self.capabilities:
            if cap.feature == feature:
                cap.enabled = True

    def disable_feature(self, feature: Feature) -> None:
        """Disable a feature and its associated capabilities."""
        self.enabled_features.discard(feature)
        for cap in self.capabilities:
            if cap.feature == feature:
                cap.enabled = False


class StudentSystemBuilder:
    """Builder for constructing a StudentSystem with default or custom configurations."""

    def __init__(self) -> None:
        self._name = "PaiCLI Student Management System"
        self._version = "1.0.0"
        self._enabled_features: set[Feature] = set()
        self._capabilities: list[SystemCapability] = []
        self._config: dict[str, Any] = {}

    def with_name(self, name: str) -> StudentSystemBuilder:
        """Set the system name."""
        self._name = name
        return self

    def with_version(self, version: str) -> StudentSystemBuilder:
        """Set the system version."""
        self._version = version
        return self

    def with_features(self, *features: Feature) -> StudentSystemBuilder:
        """Enable the specified features."""
        self._enabled_features.update(features)
        return self

    def with_all_features(self) -> StudentSystemBuilder:
        """Enable every available feature."""
        self._enabled_features = set(Feature)
        return self

    def with_capability(self, capability: SystemCapability) -> StudentSystemBuilder:
        """Add a custom capability."""
        self._capabilities.append(capability)
        return self

    def with_config(self, key: str, value: Any) -> StudentSystemBuilder:
        """Set a configuration value."""
        self._config[key] = value
        return self

    def build(self) -> StudentSystem:
        """Construct the StudentSystem instance."""
        return StudentSystem(
            name=self._name,
            version=self._version,
            enabled_features=set(self._enabled_features),
            capabilities=list(self._capabilities),
            config=dict(self._config),
        )

    @staticmethod
    def default() -> StudentSystem:
        """Create a StudentSystem with sensible defaults.

        Enables core features: registration, lookup, update, course management,
        enrollment, and grade recording.
        """
        return (
            StudentSystemBuilder()
            .with_name("PaiCLI Student Management System")
            .with_version("1.0.0")
            .with_features(
                Feature.STUDENT_REGISTRATION,
                Feature.STUDENT_LOOKUP,
                Feature.STUDENT_UPDATE,
                Feature.COURSE_MANAGEMENT,
                Feature.COURSE_ENROLLMENT,
                Feature.GRADE_RECORDING,
                Feature.GPA_CALCULATION,
                Feature.AUDIT_LOGGING,
            )
            .with_capability(
                SystemCapability(
                    name="create_student",
                    description="Register a new student in the system",
                    feature=Feature.STUDENT_REGISTRATION,
                )
            )
            .with_capability(
                SystemCapability(
                    name="get_student",
                    description="Look up a student by ID",
                    feature=Feature.STUDENT_LOOKUP,
                )
            )
            .with_capability(
                SystemCapability(
                    name="update_student",
                    description="Update student details",
                    feature=Feature.STUDENT_UPDATE,
                )
            )
            .with_capability(
                SystemCapability(
                    name="list_courses",
                    description="List available courses",
                    feature=Feature.COURSE_MANAGEMENT,
                )
            )
            .with_capability(
                SystemCapability(
                    name="enroll_student",
                    description="Enroll a student in a course",
                    feature=Feature.COURSE_ENROLLMENT,
                )
            )
            .with_capability(
                SystemCapability(
                    name="record_grade",
                    description="Record a grade for a student",
                    feature=Feature.GRADE_RECORDING,
                )
            )
            .with_capability(
                SystemCapability(
                    name="calculate_gpa",
                    description="Calculate a student's GPA",
                    feature=Feature.GPA_CALCULATION,
                )
            )
            .build()
        )
