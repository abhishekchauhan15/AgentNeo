from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import ast
from tqdm import tqdm

from ..data import (
    ProjectInfoModel,
    TraceModel,
    MetricModel,
)

from .metrics import (
    execute_goal_decomposition_efficiency_metric,
    execute_goal_fulfillment_metric,
    execute_tool_call_correctness_rate,
    execute_tool_call_success_rate,
    execute_tool_selection_accuracy_metric,
    execute_tool_usage_efficiency_metric,
    execute_plan_adaptibility_metric,
)

class Evaluation:
    def __init__(self, session, trace_id):
        self.user_session = session
        self.project_name = session.project_name
        self.trace_id = trace_id

        # Setup DB
        self.db_path = self.user_session.db_path
        self.engine = create_engine(self.db_path)
        self.session = Session(bind=self.engine)

        self.trace_data = self.get_trace_data()

    def evaluate(self, metric_list=[], config={}, metadata={}, max_workers=None):
        """
        Evaluate a list of metrics in parallel with progress tracking.
        
        Parameters:
        - metric_list: List of metrics to evaluate.
        - config: Configuration settings for the evaluation.
        - metadata: Metadata for the evaluation.
        - max_workers: The maximum number of workers to use for parallel execution.
        If None, it will use the default number of workers based on the system.
        """
        results = []
        
        # Uses ThreadPoolExecutor with max_workers if provided
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all metrics for parallel execution
            future_to_metric = {
                executor.submit(self._execute_and_save_metric, metric, config, metadata): metric
                for metric in metric_list
            }

            # Initialize progress bar
            with tqdm(total=len(future_to_metric), desc="Evaluating Metrics") as pbar:
                # Collect results as they are completed
                for future in as_completed(future_to_metric):
                    metric = future_to_metric[future]
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        print(f"Metric {metric} failed with exception: {e}")
                    finally:
                        pbar.update(1)  # Update the progress bar after each metric completion

        # Commit session and close it
        self.session.commit()
        self.session.close()

    def _execute_and_save_metric(self, metric, config, metadata):
        """
        Execute a metric and save its result.
        """
        start_time = datetime.now()
        try:
            result = self._execute_metric(metric, config, metadata)
        except ValueError as e:
            print(f"Error executing metric {metric}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error while executing metric {metric}: {e}")
            return None
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        if result:
            self._save_metric_result(metric, result, start_time, end_time, duration)
        return result

    def _execute_metric(self, metric, config, metadata):
        if metric == 'goal_decomposition_efficiency':
            return execute_goal_decomposition_efficiency_metric(
                trace_json=self.trace_data,
                config=config,
                metadata=metadata,
            )
        elif metric == 'goal_fulfillment_rate':
            return execute_goal_fulfillment_metric(
                trace_json=self.trace_data,
                config=config,
            )
        elif metric == 'tool_call_correctness_rate':
            return execute_tool_call_correctness_rate(
                trace_json=self.trace_data,
                config=config,
            )
        elif metric == 'tool_call_success_rate':
            return execute_tool_call_success_rate(
                trace_json=self.trace_data,
                config=config,
            )
        else:
            raise ValueError("provided metric name is not supported.")

    def _save_metric_result(self, metric, result, start_time, end_time, duration):
        metric_entry = MetricModel(
            trace_id=self.trace_id,
            metric_name=metric,
            score=result['result']['score'],
            reason=result['result']['reason'],
            result_detail=result,
            config=result['config'],
            start_time=start_time,
            end_time=end_time,
            duration=duration
        )
        self.session.add(metric_entry)

    def get_results(self):
        results = self.session.query(MetricModel).filter_by(trace_id=self.trace_id).all()
        results_list = []
        for result in results:
            result_dict = {
                'metric_name': result.metric_name,
                'score': result.score,
                'reason': result.reason,
                'result_detail': result.result_detail,
                'config': result.config,
                'start_time': result.start_time.isoformat() if result.start_time else None,
                'end_time': result.end_time.isoformat() if result.end_time else None,
                'duration': result.duration
            }
            results_list.append(result_dict)
        
        return results_list

    def get_trace_data(self):
        try:
            # Query the trace with the specific ID
            trace = self.session.query(TraceModel).filter_by(id=self.trace_id).one_or_none()

            json_data = []
            if trace:
                # Serialize the trace and write it as JSON
                trace_dict = self.serialize_trace(trace)
                json_data.append(trace_dict)  # Store the trace dictionary
            else:
                raise ValueError(f"No trace found with ID {self.trace_id}")
            
            return json_data[0]

        except Exception as e:
            raise ValueError(f"Unable to load the trace data: {e}")

    def serialize_trace(self, trace):
        """Convert a TraceModel object into a dictionary, including all related objects."""
        return {
            'id': trace.id,
            'project_id': trace.project_id,
            'start_time': trace.start_time.isoformat() if trace.start_time else None,
            'end_time': trace.end_time.isoformat() if trace.end_time else None,
            'duration': trace.duration,
            'project': trace.project.project_name if trace.project else None,
            'system_info': {
                'os_name': trace.system_info.os_name,
                'os_version': trace.system_info.os_version,
                'python_version': trace.system_info.python_version,
                'cpu_info': trace.system_info.cpu_info,
                'gpu_info': trace.system_info.gpu_info,
                'disk_info': self.parse_json_field(trace.system_info.disk_info),
                'memory_total': trace.system_info.memory_total,
                'installed_packages': self.parse_json_field(trace.system_info.installed_packages)
            } if trace.system_info else None,
            'agent_calls': [
                {
                    'id': agent_call.id,
                    'name': agent_call.name,
                    'start_time': agent_call.start_time.isoformat() if agent_call.start_time else None,
                    'end_time': agent_call.end_time.isoformat() if agent_call.end_time else None,
                    'llm_call_ids': self.parse_json_field(agent_call.llm_call_ids),
                    'tool_call_ids': self.parse_json_field(agent_call.tool_call_ids),
                    'user_interaction_ids': self.parse_json_field(agent_call.user_interaction_ids),
                } for agent_call in trace.agent_calls
            ],
            'llm_calls': [
                {
                    'id': llm_call.id,
                    'name': llm_call.name,
                    'model': llm_call.model,
                    'input_prompt': self.parse_json_field(llm_call.input_prompt),
                    'output': self.parse_json_field(llm_call.output),
                    'tool_call': self.parse_json_field(llm_call.tool_call),
                    'start_time': llm_call.start_time.isoformat() if llm_call.start_time else None,
                    'end_time': llm_call.end_time.isoformat() if llm_call.end_time else None,
                    'duration': llm_call.duration,
                    'token_usage': self.parse_json_field(llm_call.token_usage),
                    'cost': self.parse_json_field(llm_call.cost),
                    'memory_used': llm_call.memory_used
                } for llm_call in trace.llm_calls
            ],
            'tool_calls': [
                {
                    'id': tool_call.id,
                    'name': tool_call.name,
                    'input_parameters': self.parse_json_field(tool_call.input_parameters),
                    'output': self.parse_json_field(tool_call.output),
                    'start_time': tool_call.start_time.isoformat() if tool_call.start_time else None,
                    'end_time': tool_call.end_time.isoformat() if tool_call.end_time else None,
                    'duration': tool_call.duration,
                    'memory_used': tool_call.memory_used,
                    'network_calls': self.parse_json_field(tool_call.network_calls)
                } for tool_call in trace.tool_calls
            ],
            'errors': [
                {
                    'id': error.id,
                    'error_type': error.error_type,
                    'error_message': error.error_message,
                    'timestamp': error.timestamp.isoformat() if error.timestamp else None
                } for error in trace.errors
            ],
            'user_interactions': [
                {
                    'id': interaction.id,
                    'interaction_type': interaction.interaction_type,
                    'content': interaction.content,
                    'timestamp': interaction.timestamp.isoformat() if interaction.timestamp else None
                } for interaction in trace.user_interactions
            ]
        }

    def parse_json_field(self, field):
        """Parse a JSON string field into a Python object, if necessary."""
        if isinstance(field, str):
            try:
                return json.loads(field)
            except json.JSONDecodeError:
                try:
                    return ast.literal_eval(field)
                except:
                    return field
        elif isinstance(field, (list, dict)):
            return field
        return field