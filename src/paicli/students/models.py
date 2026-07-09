"""Student data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class GradeLevel(Enum):
    """Academic grade levels."""

    FRESHMAN = "freshman"
    SOPHOMORE = "sophomore"
    JUNIOR = "junior"
    SENIOR = "senior"
    GRADUATE = "graduate"
    POSTGRADUATE = "postgraduate"


class StudentStatus(Enum):
    """Current status of a student in the system."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    GRADUATED = "graduated"
    WITHDRAWN = "withdrawn"
    PROBATION = "probation"


class EnrollmentStatus(Enum):
    """Status of a course enrollment."""

    ENROLLED = "enrolled"
    WAITLISTED = "waitlisted"
    DROPPED = "dropped"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"


@dataclass(slots=True)
class Student:
    """Represents a student in the education system.

    Attributes:
        student_id: Unique identifier for the student.
        first_name: Student's first name.
        last_name: Student's last name.
        email: Student's email address.
        date_of_birth: Student's date of birth.
        grade_level: Current academic grade level.
        status: Current status in the system.
        enrollment_date: Date when the student first enrolled.
        gpa: Current grade point average.
        credits_earned: Total credits earned.
        majors: List of declared majors.
        minors: List of declared minors.
        metadata: Extra key-value pairs for extensibility.
    """

    student_id: str
    first_name: str
    last_name: str
    email: str
    date_of_birth: date | None = None
    grade_level: GradeLevel = GradeLevel.FRESHMAN
    status: StudentStatus = StudentStatus.ACTIVE
    enrollment_date: date | None = None
    gpa: float = 0.0
    credits_earned: int = 0
    majors: list[str] = field(default_factory=list)
    minors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        """Return the student's full name."""
        return f"{self.first_name} {self.last_name}"


@dataclass(slots=True)
class Course:
    """Represents a course offered in the system.

    Attributes:
        course_id: Unique course identifier (e.g., 'CS101').
        title: Course title.
        description: Brief course description.
        credits: Number of credits awarded.
        department: Academic department offering the course.
        instructor: Name of the instructor.
        max_enrollment: Maximum number of students allowed.
        prerequisites: List of course IDs required before enrollment.
        schedule: Class schedule description (e.g., 'MWF 10:00-10:50').
        location: Physical or virtual location.
        is_active: Whether the course is currently offered.
    """

    course_id: str
    title: str
    description: str = ""
    credits: int = 3
    department: str = ""
    instructor: str = ""
    max_enrollment: int = 30
    prerequisites: list[str] = field(default_factory=list)
    schedule: str = ""
    location: str = ""
    is_active: bool = True


@dataclass(slots=True)
class Enrollment:
    """Links a student to a course they are enrolled in.

    Attributes:
        enrollment_id: Unique identifier for this enrollment record.
        student_id: Reference to the enrolled student.
        course_id: Reference to the course.
        semester: Academic semester (e.g., '2025-Fall').
        status: Current enrollment status.
        grade: Letter grade awarded (e.g., 'A', 'B+'), empty if not yet graded.
        enrolled_at: Date the enrollment was created.
    """

    enrollment_id: str
    student_id: str
    course_id: str
    semester: str
    status: EnrollmentStatus = EnrollmentStatus.ENROLLED
    grade: str = ""
    enrolled_at: date | None = None


@dataclass(slots=True)
class Grade:
    """Represents a grade record for a student in a course.

    Attributes:
        grade_id: Unique identifier for the grade record.
        student_id: Reference to the student.
        course_id: Reference to the course.
        semester: Academic semester.
        letter_grade: Letter grade (A, B+, etc.).
        numeric_grade: Numeric score (0.0 - 100.0).
        credits: Credits this grade applies to.
        comments: Instructor comments.
        graded_at: Date the grade was recorded.
    """

    grade_id: str
    student_id: str
    course_id: str
    semester: str
    letter_grade: str = ""
    numeric_grade: float = 0.0
    credits: int = 3
    comments: str = ""
    graded_at: date | None = None
