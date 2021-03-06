from typing import List, Optional

import optuna
import requests
from lightning import BuildConfig, LightningFlow
from lightning.app.frontend import StreamlitFrontend
from lightning.app.storage import Drive
from lightning.app.storage.path import Path
from lightning.app.structures import Dict

from lightning_hpo import Sweep
from lightning_hpo.algorithm import OptunaAlgorithm
from lightning_hpo.commands.sweep import SweepCommand, SweepConfig
from lightning_hpo.components.servers.db.models import Trial
from lightning_hpo.components.servers.db.server import Database
from lightning_hpo.components.servers.db.visualization import DatabaseViz
from lightning_hpo.components.servers.file_server import FileServer
from lightning_hpo.utilities.utils import CloudCompute, get_best_model_path


class Sweeper(LightningFlow):
    def __init__(self, use_db_viz: bool = True):
        super().__init__()
        self.sweeps = Dict()
        self.drive = Drive("lit://code")
        self.file_server = FileServer(self.drive)
        self.db = Database()
        self.db_viz = DatabaseViz()

    def run(self):
        self.file_server.run()
        self.db.run()
        self.db_viz.run()
        if self.file_server.alive() and self.db.alive():
            trials = []
            for sweep in self.sweeps.values():
                sweep.run()
                trials.extend(sweep.get_trials())
            if trials:
                for trial in trials:
                    requests.post(self.db.url + "/trial", data=trial.json())

    def create_sweep(self, config: SweepConfig) -> str:
        sweep_ids = list(self.sweeps.keys())
        if config.sweep_id not in sweep_ids:
            self.sweeps[config.sweep_id] = Sweep(
                script_path=config.script_path,
                n_trials=config.n_trials,
                simultaneous_trials=config.simultaneous_trials,
                framework=config.framework,
                script_args=config.script_args,
                distributions=config.distributions,
                cloud_compute=CloudCompute(config.cloud_compute, config.num_nodes),
                sweep_id=config.sweep_id,
                code={"drive": self.drive, "name": config.sweep_id},
                cloud_build_config=BuildConfig(requirements=config.requirements),
                logger=config.logger,
                algorithm=OptunaAlgorithm(optuna.create_study(direction=config.direction)),
            )
            return f"Launched a sweep {config.sweep_id}"
        elif self.sweeps[config.sweep_id].has_failed:
            self.sweeps[config.sweep_id].restart_count += 1
            self.sweeps[config.sweep_id].has_failed = False
            return f"Updated code for Sweep {config.sweep_id}."
        else:
            return f"The current Sweep {config.sweep_id} is running. It couldn't be updated."

    def configure_commands(self):
        return [{"sweep": SweepCommand(self.create_sweep)}]

    @property
    def best_model_score(self) -> Optional[Path]:
        return get_best_model_path(self)

    def configure_layout(self):
        return StreamlitFrontend(render_fn=render_fn)


def render_fn(state):
    import streamlit as st
    import streamlit.components.v1 as components
    from sqlmodel import create_engine, select, Session

    if "database" not in st.session_state:
        engine = create_engine(f"sqlite:///{state.db.db_file_name}", echo=True)
        st.session_state["engine"] = engine

    with Session(st.session_state["engine"]) as session:
        trials: List[Trial] = session.exec(select(Trial)).all()

    if not trials:
        st.header("You haven't launched any sweeps yet.")
        st.write("Here is an example to submit a sweep.")
        st.code(
            'lightning sweep train.py --n_trials=2 --num_nodes=2 --model.lr="log_uniform(0.001, 0.1)" --trainer.max_epochs=5 --trainer.callbacks=ModelCheckpoint'
        )
        return

    user_sweeps = {}
    for trial in trials:
        username, sweep_id = trial.sweep_id.split("-")
        if username not in user_sweeps:
            user_sweeps[username] = {}
        if sweep_id not in user_sweeps[username]:
            user_sweeps[username][sweep_id] = []
        user_sweeps[username][sweep_id].append(trial)

    user_tabs = st.tabs(list(user_sweeps))
    for tab, username in zip(user_tabs, user_sweeps):
        with tab:
            for sweep_id, trials in user_sweeps[username].items():
                status = "/ Succeeded" if trial.has_succeeded else "/ Failed"
                with st.expander(f"{sweep_id} {status}"):
                    trials_tab, logging_tab = st.tabs(["Trials", "Logging"])
                    with trials_tab:
                        for trial in trials:
                            if st.checkbox(f"Trial {trial.trial_id}", key=f"checkbox_{trial.id}_{sweep_id}"):
                                st.json(
                                    {
                                        "params": trial.params,
                                        "monitor": trial.monitor,
                                        "best_model_score": trial.best_model_score,
                                    }
                                )
                    with logging_tab:
                        components.html(f'<a href="{trial.url}" target="_blank">Weights & Biases URL</a>', height=50)


class HPOSweeper(LightningFlow):
    def __init__(self):
        super().__init__()
        self.sweeper = Sweeper()

    def run(self):
        self.sweeper.run()

    def configure_layout(self):
        tabs = [{"name": "Team Control", "content": self.sweeper}]
        tabs += [{"name": "Database Viz", "content": self.sweeper.db_viz}]
        for sweep in self.sweeper.sweeps.values():
            if sweep.show:
                tabs += sweep.configure_layout()
        return tabs

    def configure_commands(self):
        return self.sweeper.configure_commands()
