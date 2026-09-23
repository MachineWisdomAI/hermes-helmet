"""Build portable skill assets from their sole editable source in skills/."""
from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def skill_mapping(self):
        root = Path("skills")
        target = Path(self.build_lib) / "hermes_helmet" / "bundled_skills"
        return {
            str(target / source.relative_to(root)): str(source)
            for skill in sorted(root.iterdir())
            if (skill / "SKILL.md").is_file()
            for source in sorted(skill.rglob("*"))
            if source.is_file()
        }

    def run(self):
        super().run()
        if self.editable_mode or self.dry_run:
            return
        # Remove stale assets when a skill or reference is deleted or renamed.
        target = Path(self.build_lib) / "hermes_helmet" / "bundled_skills"
        if target.exists():
            shutil.rmtree(target)
        for destination, source in self.skill_mapping().items():
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            self.copy_file(source, destination)

    def get_source_files(self):
        return super().get_source_files() + list(self.skill_mapping().values())

    def get_outputs(self, include_bytecode=1):
        return super().get_outputs(include_bytecode) + list(self.skill_mapping())

    def get_output_mapping(self):
        return {**super().get_output_mapping(), **self.skill_mapping()}


setup(cmdclass={"build_py": BuildPy})
