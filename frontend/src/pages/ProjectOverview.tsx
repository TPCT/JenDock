import { useParams } from "react-router-dom";
import { useProject } from "../api/hooks";
import { ENVIRONMENTS } from "../api/types";
import { EnvironmentCard } from "../components/EnvironmentCard";
import { HostCard } from "../components/HostCard";
import box from "../styles/box.module.css";
import styles from "./Projects.module.css";

/** Overview tab: both environments side by side, with the last run of each. */
export function ProjectOverviewPage() {
  const { slug = "" } = useParams();
  const project = useProject(slug);
  const data = project.data;
  if (!data) return null;
  return (
    <div className={box.stack}>
      <div className={styles.envs}>
        {ENVIRONMENTS.map((name) => {
          const environment = data.environments.find((item) => item.name === name);
          return environment ? (
            <div key={name} className={`${box.box} ${box.row}`}>
              <EnvironmentCard project={data} environment={environment} detailed />
            </div>
          ) : null;
        })}
      </div>
      <HostCard />
    </div>
  );
}
